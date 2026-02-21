import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from atproto import Client


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def must_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env: {name}")
    return v


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def youtube_get_live_video_id(api_key: str, channel_id: str) -> Optional[str]:
    params = {
        "key": api_key,
        "part": "id",
        "channelId": channel_id,
        "eventType": "live",
        "type": "video",
        "maxResults": 1,
        "order": "date",
    }
    r = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    items = data.get("items", [])
    if not items:
        return None

    video_id = (items[0].get("id") or {}).get("videoId")
    if not video_id:
        return None
    return str(video_id)


def build_message(template: str, video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        template
        .replace("{url}", url)
        .replace("{video_id}", video_id)
        .replace("{now}", now)
    )


def post_to_bluesky(handle: str, app_password: str, text: str) -> None:
    client = Client()
    client.login(handle, app_password)
    client.send_post(text)


def main() -> int:
    yt_api_key = must_env("YOUTUBE_API_KEY")
    yt_channel_id = must_env("YOUTUBE_CHANNEL_ID")
    bsky_handle = must_env("BLUESKY_HANDLE")
    bsky_app_password = must_env("BLUESKY_APP_PASSWORD")

    state_path = os.getenv("STATE_PATH", ".state/state.json")
    template = os.getenv("MESSAGE_TEMPLATE", "配信開始しました {url}")

    state = load_state(state_path)
    last_notified = state.get("last_notified_video_id")

    try:
        live_video_id = youtube_get_live_video_id(yt_api_key, yt_channel_id)
    except Exception as e:
        print(f"ERROR: YouTube API call failed: {e}", file=sys.stderr)
        return 2

    if not live_video_id:
        print("No live broadcast detected.")
        return 0

    if live_video_id == last_notified:
        print(f"Already notified for video_id={live_video_id}")
        return 0

    msg = build_message(template, live_video_id)

    try:
        post_to_bluesky(bsky_handle, bsky_app_password, msg)
    except Exception as e:
        print(f"ERROR: Bluesky post failed: {e}", file=sys.stderr)
        return 3

    state["last_notified_video_id"] = live_video_id
    save_state(state_path, state)
    print(f"Notified and saved state for video_id={live_video_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())