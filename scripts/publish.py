#!/usr/bin/env python3
"""
publish.py
Publishes the best queued Reddit meme to Instagram.

Reads:
    data/discovery_queue.json
    data/posted.json

Writes:
    data/discovery_queue.json
    data/posted.json

Required GitHub repository secrets:
    IG_USER_ID
    IG_ACCESS_TOKEN

Optional environment variable:
    DRY_RUN=true
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
QUEUE_FILE = DATA_DIR / "discovery_queue.json"
POSTED_FILE = DATA_DIR / "posted.json"

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MAX_UPLOAD_ATTEMPTS = 10
SECONDS_BETWEEN_STATUS_CHECKS = 5


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


def graph_request(endpoint, params=None, method="GET"):
    url = f"{GRAPH_API_BASE}/{endpoint}"

    if params is None:
        params = {}

    params["access_token"] = IG_ACCESS_TOKEN

    if method == "GET":
        url = f"{url}?{urlencode(params)}"
        request = Request(url, method="GET")
    else:
        data = urlencode(params).encode("utf-8")
        request = Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
        )

    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_best_queued_meme(queue):
    queued = [
        item
        for item in queue
        if isinstance(item, dict)
        and item.get("status") == "queued"
        and item.get("image_url")
    ]

    if not queued:
        return None

    queued.sort(
        key=lambda item: item.get("viral_score", 0),
        reverse=True,
    )

    return queued[0]


def create_media_container(meme):
    endpoint = f"{IG_USER_ID}/media"

    params = {
        "image_url": meme["image_url"],
        "caption": meme.get("ig_caption", ""),
    }

    response = graph_request(endpoint, params, method="POST")

    if "id" not in response:
        raise RuntimeError(f"Instagram did not return a container ID: {response}")

    return response["id"]


def wait_until_container_ready(container_id):
    endpoint = container_id

    for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
        response = graph_request(
            endpoint,
            {"fields": "status_code"},
            method="GET",
        )

        status = response.get("status_code", "")
        print(f"Container status check {attempt}: {status}")

        if status == "FINISHED":
            return True

        if status in ["ERROR", "EXPIRED"]:
            raise RuntimeError(f"Instagram container failed: {response}")

        time.sleep(SECONDS_BETWEEN_STATUS_CHECKS)

    raise RuntimeError("Instagram container did not become ready in time.")


def publish_container(container_id):
    endpoint = f"{IG_USER_ID}/media_publish"

    params = {
        "creation_id": container_id,
    }

    response = graph_request(endpoint, params, method="POST")

    if "id" not in response:
        raise RuntimeError(f"Instagram publish failed: {response}")

    return response["id"]


def mark_as_posted(queue, meme, instagram_media_id, container_id):
    meme["status"] = "posted"
    meme["posted_at"] = datetime.now(timezone.utc).isoformat()
    meme["instagram_media_id"] = instagram_media_id
    meme["instagram_container_id"] = container_id

    posted_data = load_json(POSTED_FILE, {"posted": []})

    if not isinstance(posted_data, dict):
        posted_data = {"posted": []}

    posted_data.setdefault("posted", []).append(
        {
            "id": meme.get("id"),
            "subreddit": meme.get("subreddit"),
            "title": meme.get("title"),
            "permalink": meme.get("permalink"),
            "image_url": meme.get("image_url"),
            "viral_score": meme.get("viral_score"),
            "instagram_media_id": instagram_media_id,
            "instagram_container_id": container_id,
            "posted_at": meme["posted_at"],
        }
    )

    save_json(QUEUE_FILE, queue)
    save_json(POSTED_FILE, posted_data)


def mark_as_failed(queue, meme, error_message):
    meme["status"] = "failed"
    meme["failed_at"] = datetime.now(timezone.utc).isoformat()
    meme["error"] = str(error_message)

    save_json(QUEUE_FILE, queue)


def main():
    if not IG_USER_ID or not IG_ACCESS_TOKEN:
        raise RuntimeError(
            "Missing IG_USER_ID or IG_ACCESS_TOKEN. "
            "Add both as GitHub repository secrets."
        )

    queue = load_json(QUEUE_FILE, [])
    meme = get_best_queued_meme(queue)

    if not meme:
        print("No queued memes available.")
        return

    print(f"Selected meme: {meme.get('title', '')[:100]}")
    print(f"Viral score: {meme.get('viral_score')}")
    print(f"Image URL: {meme.get('image_url')}")

    if DRY_RUN:
        print("DRY_RUN=true — no Instagram post was created.")
        return

    try:
        print("Creating Instagram media container...")
        container_id = create_media_container(meme)
        print(f"Container created: {container_id}")

        print("Waiting for Instagram to process the image...")
        wait_until_container_ready(container_id)

        print("Publishing to Instagram...")
        instagram_media_id = publish_container(container_id)

        mark_as_posted(
            queue=queue,
            meme=meme,
            instagram_media_id=instagram_media_id,
            container_id=container_id,
        )

        print(f"Successfully published Instagram media: {instagram_media_id}")

    except Exception as error:
        print(f"Publishing failed: {error}")
        mark_as_failed(queue, meme, error)
        raise


if __name__ == "__main__":
    main()
