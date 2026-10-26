#!/usr/bin/env python3
"""Collect YouTube rap candidates. Never publishes content."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "rap_sources.json"
STATE_PATH = ROOT / "data" / "rap_state.json"
REPORT_PATH = ROOT / "data" / "rap_candidates.json"
API_BASE = "https://www.googleapis.com/youtube/v3/"


def read_json(path, default=None):
    if not path.exists():
        return default

    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def iso_time(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def api_get(endpoint, params, key):
    url = API_BASE + endpoint + "?" + urlencode(params)
    request = Request(url, headers={"X-Goog-Api-Key": key})

    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    except HTTPError as error:
        message = "Unbekannter API-Fehler"

        try:
            payload = json.loads(error.read().decode("utf-8"))
            details = payload.get("error", {})
            message = details.get("message", message)
        except (ValueError, UnicodeDecodeError):
            pass

        message = str(message).replace(key, "[REDACTED]")
        raise RuntimeError(
            f"YouTube {endpoint}: HTTP {error.code}: {message}"
        ) from None

    except (URLError, TimeoutError):
        raise RuntimeError(
            f"YouTube {endpoint} ist momentan nicht erreichbar."
        ) from None


def add_source(sources, video_id, market, source):
    if not video_id:
        return

    entry = sources.setdefault(
        video_id,
        {"market_candidates": set(), "sources": set()},
    )
    entry["market_candidates"].add(market)
    entry["sources"].add(source)


def collect_sources(config, state, key, now):
    settings = config["discovery"]
    sources = {}

    if settings.get("track_previously_discovered_videos", True):
        for video_id, item in state["videos"].items():
            sources[video_id] = {
                "market_candidates": set(
                    item.get("market_candidates", [])
                ),
                "sources": set(item.get("sources", [])),
            }

    cutoff = now - timedelta(
        days=settings.get("recent_search_days", 30)
    )

    for market in config["markets"]:
        market_id = market["id"]
        print(f"Suche: {market['label']}", flush=True)

        common = {
            "part": "snippet",
            "type": "video",
            "regionCode": market["region_code"],
            "relevanceLanguage": market["relevance_language"],
            "publishedAfter": iso_time(cutoff),
            "maxResults": settings.get("results_per_query", 15),
        }

        for query in market["search_queries"]:
            for order in settings.get(
                "search_orders", ["date", "viewCount"]
            ):
                result = api_get(
                    "search",
                    dict(common, q=query, order=order),
                    key,
                )

                for item in result.get("items", []):
                    add_source(
                        sources,
                        item.get("id", {}).get("videoId"),
                        market_id,
                        f"search:{market_id}:{order}:{query}",
                    )

        # Only use channel IDs explicitly supplied in the config.
        for channel_id in market.get("verified_channel_ids", []):
            result = api_get(
                "search",
                dict(common, channelId=channel_id, order="date"),
                key,
            )

            for item in result.get("items", []):
                add_source(
                    sources,
                    item.get("id", {}).get("videoId"),
                    market_id,
                    f"channel:{channel_id}",
                )

        if settings.get("include_music_charts", True):
            result = api_get(
                "videos",
                {
                    "part": "snippet",
                    "chart": "mostPopular",
                    "regionCode": market["region_code"],
                    "videoCategoryId": "10",
                    "maxResults": settings.get(
                        "chart_results_per_market", 25
                    ),
                },
                key,
            )

            for item in result.get("items", []):
                add_source(
                    sources,
                    item.get("id"),
                    market_id,
                    f"music_chart:{market_id}",
                )

    return sources


def collect_details(video_ids, key):
    result = []

    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]

        payload = api_get(
            "videos",
            {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
            },
            key,
        )

        result.extend(payload.get("items", []))

    return result


def build_results(config, state, sources, details, now):
    settings = config["discovery"]
    minimum_hours = float(
        settings.get("minimum_hours_between_measurements", 6)
    )

    if minimum_hours <= 0:
        raise ValueError(
            "minimum_hours_between_measurements muss positiv sein."
        )

    updated_videos = {}
    candidates = []

    for video in details:
        video_id = video["id"]
        snippet = video["snippet"]

        # Do not treat active/upcoming livestreams as normal song videos.
        if snippet.get("liveBroadcastContent") in ("live", "upcoming"):
            continue

        statistics = video.get("statistics", {})

        if "viewCount" not in statistics:
            continue

        views = int(statistics["viewCount"])
        published = parse_time(snippet["publishedAt"])
        age_hours = max(
            (now - published).total_seconds() / 3600, 1
        )

        previous = state["videos"].get(video_id, {})
        anchor = previous.get("anchor")
        last_interval = previous.get("last_interval")

        measurement_status = "baseline"
        interval = None

        if anchor:
            elapsed = (
                now - parse_time(anchor["measured_at"])
            ).total_seconds() / 3600

            if elapsed < 0:
                raise ValueError(
                    "Ein gespeicherter Messzeitpunkt liegt in der Zukunft."
                )

            if views < int(anchor["views"]):
                anchor = {
                    "measured_at": iso_time(now),
                    "views": views,
                }
                last_interval = None
                measurement_status = "counter_reset"

            elif elapsed >= minimum_hours:
                delta = views - int(anchor["views"])
                rate = delta / elapsed
                previous_rate = (
                    last_interval.get("views_per_hour")
                    if last_interval else None
                )

                acceleration = None
                if previous_rate is not None and previous_rate > 0:
                    acceleration = round(rate / previous_rate, 3)

                interval = {
                    "from": anchor["measured_at"],
                    "to": iso_time(now),
                    "hours": round(elapsed, 3),
                    "views_gained": delta,
                    "views_per_hour": round(rate, 3),
                    "rate_ratio_vs_previous_interval": acceleration,
                }

                last_interval = interval
                anchor = {
                    "measured_at": iso_time(now),
                    "views": views,
                }
                measurement_status = "measured"

            else:
                measurement_status = "waiting_for_interval"

        else:
            anchor = {
                "measured_at": iso_time(now),
                "views": views,
            }

        source = sources[video_id]
        markets = sorted(source["market_candidates"])
        evidence = sorted(source["sources"])

        updated_videos[video_id] = {
            "first_seen_at": previous.get(
                "first_seen_at", iso_time(now)
            ),
            "last_seen_at": iso_time(now),
            "market_candidates": markets,
            "sources": evidence,
            "anchor": anchor,
            "last_interval": last_interval,
        }

        candidates.append({
            "video_id": video_id,
            "title": snippet["title"],
            "channel": snippet["channelTitle"],
            "channel_id": snippet["channelId"],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": snippet["publishedAt"],
            "duration": video.get(
                "contentDetails", {}
            ).get("duration"),
            "views": views,
            "likes": (
                int(statistics["likeCount"])
                if "likeCount" in statistics else None
            ),
            "comments": (
                int(statistics["commentCount"])
                if "commentCount" in statistics else None
            ),
            "lifetime_average_views_per_hour": round(
                views / age_hours, 3
            ),
            "measurement_status": measurement_status,
            "current_interval": interval,
            "last_completed_interval": last_interval,
            "market_candidates": markets,
            "sources": evidence,
            "genre_status": "needs_review",
            "rights_status": "not_checked",
            "publish_status": "blocked",
        })

    # Measured momentum first; otherwise use a clearly labelled proxy.
    def ranking(item):
        interval = item["current_interval"]
        if interval is not None:
            return (1, interval["views_per_hour"])

        return (0, item["lifetime_average_views_per_hour"])

    candidates.sort(key=ranking, reverse=True)

    return updated_videos, candidates


def main():
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise ValueError("YOUTUBE_API_KEY fehlt.")

    config = read_json(CONFIG_PATH)
    if not config or not config.get("markets"):
        raise ValueError("Rap-Konfiguration fehlt oder ist unvollstaendig.")

    state = read_json(
        STATE_PATH, {"schema_version": 1, "videos": {}}
    )
    if not isinstance(state.get("videos"), dict):
        raise ValueError("rap_state.json hat ein ungueltiges Format.")

    now = datetime.now(timezone.utc)
    sources = collect_sources(config, state, key, now)

    if not sources:
        raise RuntimeError(
            "Keine Kandidaten gefunden. Vorhandene Daten bleiben erhalten."
        )

    details = collect_details(sorted(sources), key)

    if not details:
        raise RuntimeError(
            "Keine Videodetails erhalten. Daten bleiben erhalten."
        )

    videos, candidates = build_results(
        config, state, sources, details, now
    )

    if not candidates:
        raise RuntimeError(
            "Keine auswertbaren Videos. Daten bleiben erhalten."
        )

    limit = int(
        config["discovery"].get("max_candidates_in_report", 40)
    )

    report = {
        "generated_at": iso_time(now),
        "publishing_enabled": False,
        "total_tracked_videos": len(videos),
        "notes": [
            "Candidates are not confirmed rap songs.",
            "Market labels are search contexts, not artist nationality.",
            "Lifetime averages are not measured trend growth.",
            "No audio or video is downloaded.",
        ],
        "candidates": candidates[:limit],
    }

    write_json(STATE_PATH, {
        "schema_version": 1,
        "updated_at": iso_time(now),
        "videos": videos,
    })
    write_json(REPORT_PATH, report)

    print(f"Gespeicherte Videos: {len(videos)}")
    print(f"Kandidaten im Bericht: {len(report['candidates'])}")

    for index, item in enumerate(report["candidates"][:10], 1):
        print(
            f"{index:02d}. {item['title']} | "
            f"{item['views']:,} Views | "
            f"{item['measurement_status']}"
        )

    print("Keine Inhalte veroeffentlicht.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        key = os.environ.get("YOUTUBE_API_KEY", "")
        message = str(error)
        if key:
            message = message.replace(key, "[REDACTED]")
        print(f"FEHLER: {message}", file=sys.stderr)
        sys.exit(1)
