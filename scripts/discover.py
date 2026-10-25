#!/usr/bin/env python3
"""
discover.py
Finds high-potential Reddit memes and saves a publish queue.

Reads:
    data/posted.json

Writes:
    data/discovery_queue.json
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
POSTED_FILE = DATA_DIR / "posted.json"
QUEUE_FILE = DATA_DIR / "discovery_queue.json"

SUBREDDITS = [
    "memes",
    "dankmemes",
    "me_irl",
    "wholesomememes",
    "ProgrammerHumor",
    "AdviceAnimals",
]

SUBREDDIT_WEIGHTS = {
    "memes": 1.15,
    "dankmemes": 1.10,
    "me_irl": 1.20,
    "wholesomememes": 1.05,
    "programmerhumor": 0.95,
    "adviceanimals": 1.00,
}

TITLE_PATTERNS = [
    r"\bme\s+irl\b",
    r"\bwhen\b",
    r"\bnobody\b",
    r"\beveryone\b",
    r"\bpov\b",
    r"\bthe\s+way\b",
    r"\bnot\s+me\b",
    r"\bliterally\b",
]

BLOCKED_PATTERNS = [
    r"\bnsfw\b",
    r"\bnude\b",
    r"\bsex\b",
    r"\bporn\b",
    r"\bgore\b",
    r"\bdead\b",
    r"\bsuicide\b",
    r"\bpolitic",
    r"\btrump\b",
    r"\bbiden\b",
    r"\bwar\b",
    r"\bracist\b",
]

MIN_VIRAL_SCORE = 55
MAX_AGE_HOURS = 24
MAX_PER_SUBREDDIT = 3


def get_json(url):
    request = Request(
        url,
        headers={
            "User-Agent": os.getenv(
                "REDDIT_USER_AGENT",
                "auto-meme-engine/1.0",
            )
        },
    )

    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def get_posted_ids():
    posted = load_json(POSTED_FILE, {"posted": []})

    if isinstance(posted, dict):
        return {
            item.get("id")
            for item in posted.get("posted", [])
            if isinstance(item, dict) and item.get("id")
        }

    return set()


def fetch_subreddit_posts(subreddit, limit=40):
    posts = {}

    for listing in ["hot", "rising"]:
        url = (
            f"https://www.reddit.com/r/{quote_plus(subreddit)}/"
            f"{listing}.json?limit={limit}&raw_json=1"
        )

        try:
            data = get_json(url)

            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})

                if post.get("id"):
                    posts[post["id"]] = post

        except Exception as error:
            print(f"Warning: could not load r/{subreddit}/{listing}: {error}")

    return list(posts.values())


def is_image_url(url):
    path = url.lower().split("?")[0]
    return path.endswith((".jpg", ".jpeg", ".png", ".webp"))


def extract_image(post):
    if post.get("is_gallery"):
        media_metadata = post.get("media_metadata", {})

        for item in media_metadata.values():
            if item.get("status") == "valid":
                image = item.get("s", {}).get("u")

                if image:
                    return image.replace("&amp;", "&")

        return None

    url = post.get("url", "")

    if post.get("post_hint") == "image" or is_image_url(url):
        return url

    previews = post.get("preview", {}).get("images", [])

    if previews:
        resolutions = previews[0].get("resolutions", [])

        if resolutions:
            return resolutions[-1].get("url", "").replace("&amp;", "&")

    return None


def get_age_hours(post):
    created_utc = float(post.get("created_utc", time.time()))
    return max((time.time() - created_utc) / 3600, 0.01)


def calculate_viral_score(post):
    ups = max(int(post.get("ups", 0)), 0)
    comments = max(int(post.get("num_comments", 0)), 0)
    age_hours = get_age_hours(post)
    ups_per_hour = ups / age_hours
    comment_ratio = comments / max(ups, 1)

    score = 0.0
    reasons = []

    if ups_per_hour >= 1000:
        score += 45
        reasons.append("extreme upvote velocity")
    elif ups_per_hour >= 500:
        score += 35
        reasons.append("very fast upvote velocity")
    elif ups_per_hour >= 200:
        score += 25
        reasons.append("fast upvote velocity")
    elif ups_per_hour >= 80:
        score += 15
        reasons.append("moderate upvote velocity")
    elif ups_per_hour >= 25:
        score += 7
        reasons.append("early traction")

    if ups >= 20000:
        score += 20
        reasons.append("very high karma")
    elif ups >= 8000:
        score += 14
        reasons.append("high karma")
    elif ups >= 2500:
        score += 8
        reasons.append("solid karma")

    if comments >= 500:
        score += 12
        reasons.append("high comment volume")
    elif comments >= 150:
        score += 7
        reasons.append("good comment volume")

    if 0.05 <= comment_ratio <= 0.35:
        score += 5
        reasons.append("healthy comment ratio")

    if age_hours <= 3:
        score += 15
        reasons.append("very fresh")
    elif age_hours <= 8:
        score += 10
        reasons.append("fresh")
    elif age_hours <= 18:
        score += 5
        reasons.append("recent")
    elif age_hours > 36:
        score -= 10
        reasons.append("possibly past peak")

    subreddit = post.get("subreddit", "").lower()
    weight = SUBREDDIT_WEIGHTS.get(subreddit, 1.0)

    if weight != 1.0:
        score *= weight
        reasons.append(f"subreddit weight {weight:.2f}")

    title = post.get("title", "").lower()

    for pattern in TITLE_PATTERNS:
        if re.search(pattern, title):
            score += 2
            reasons.append(f"relatable title pattern: {pattern}")

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, title):
            score -= 35
            reasons.append(f"blocked keyword: {pattern}")
            break

    return round(max(score, 0.0), 2), reasons


def make_caption(title, subreddit):
    clean_title = re.sub(r"\s+", " ", title).strip()
    clean_title = re.sub(r"[#@\u2028\u2029]", "", clean_title)

    if len(clean_title) > 180:
        clean_title = clean_title[:177].rstrip() + "..."

    hashtag = subreddit.lower().replace("_", "")

    return (
        f"{clean_title}\n\n"
        "Which one are you? 👀\n\n"
        f"#meme #memes #funny #dailymemes #relatable #{hashtag}"
    )


def build_candidate(post, image_url, viral_score, reasons):
    post_id = post["id"]
    subreddit = post.get("subreddit", "memes")
    title = post.get("title", "").strip()
    age_hours = get_age_hours(post)
    ups = int(post.get("ups", 0))

    return {
        "id": post_id,
        "subreddit": subreddit,
        "title": title,
        "permalink": "https://www.reddit.com" + post.get("permalink", ""),
        "image_url": image_url,
        "ups": ups,
        "comments": int(post.get("num_comments", 0)),
        "created_utc": float(post.get("created_utc", 0)),
        "age_hours": round(age_hours, 2),
        "ups_per_hour": round(ups / age_hours, 2),
        "viral_score": viral_score,
        "reasons": reasons,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "ig_caption": make_caption(title, subreddit),
        "ig_hashtags": "#memes #funnymemes #relatablememes #dankmemes #instamemes",
        "status": "queued",
    }


def discover_memes():
    posted_ids = get_posted_ids()
    queue = load_json(QUEUE_FILE, [])
    queued_ids = {item.get("id") for item in queue if isinstance(item, dict)}

    candidates = []
    per_subreddit_count = {}

    for subreddit in SUBREDDITS:
        posts = fetch_subreddit_posts(subreddit)
        valid_posts = []

        for post in posts:
            if post.get("over_18") or post.get("spoiler"):
                continue

            image_url = extract_image(post)

            if not image_url:
                continue

            age_hours = get_age_hours(post)

            if age_hours > MAX_AGE_HOURS:
                continue

            viral_score, reasons = calculate_viral_score(post)

            if viral_score < MIN_VIRAL_SCORE:
                continue

            valid_posts.append(
                {
                    "post": post,
                    "image_url": image_url,
                    "viral_score": viral_score,
                    "reasons": reasons,
                }
            )

        valid_posts.sort(key=lambda item: item["viral_score"], reverse=True)

        for item in valid_posts:
            post = item["post"]
            post_id = post["id"]

            if post_id in posted_ids or post_id in queued_ids:
                continue

            if per_subreddit_count.get(subreddit, 0) >= MAX_PER_SUBREDDIT:
                break

            candidate = build_candidate(
                post=post,
                image_url=item["image_url"],
                viral_score=item["viral_score"],
                reasons=item["reasons"],
            )

            candidates.append(candidate)
            per_subreddit_count[subreddit] = per_subreddit_count.get(subreddit, 0) + 1

    candidates.sort(key=lambda item: item["viral_score"], reverse=True)
    return candidates


def main():
    print("Starting Reddit meme discovery...")

    candidates = discover_memes()

    if not candidates:
        print("No new memes met the viral threshold.")
        return

    queue = load_json(QUEUE_FILE, [])
    queue.extend(candidates)
    save_json(QUEUE_FILE, queue)

    print(f"Added {len(candidates)} new meme candidates.")
    print(f"Queue saved to: {QUEUE_FILE}")

    for index, candidate in enumerate(candidates, start=1):
        print(
            f"{index:02d}. "
            f"[{candidate['viral_score']:.1f}] "
            f"r/{candidate['subreddit']} | "
            f"{candidate['ups']:,} ups | "
            f"{candidate['comments']:,} comments | "
            f"{candidate['age_hours']:.1f}h"
        )
        print(f"    {candidate['title'][:100]}")
        print(f"    {candidate['permalink']}")


if __name__ == "__main__":
    main()
