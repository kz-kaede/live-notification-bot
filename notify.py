import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, List

import requests
from atproto import Client

import re
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

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


def load_template(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip() == "":
            return None
        return content
    except FileNotFoundError:
        return None


def youtube_get_live_video(api_key: str, channel_id: str) -> Tuple[Optional[str], Optional[str]]:
    params = {
        "key": api_key,
        "part": "snippet",
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
        return None, None

    item = items[0]
    video_id = (item.get("id") or {}).get("videoId")
    snippet = item.get("snippet") or {}
    title = snippet.get("title")

    return video_id, title


def build_message(template: str, video_id: str, title: str) -> Tuple[str, str]:
    url = f"https://www.youtube.com/watch?v={video_id}"

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).strftime("%Y-%m-%d %H:%M")

    text = (
        template
        .replace("{url}", url)
        .replace("{video_id}", video_id)
        .replace("{title}", title or "")
        .replace("{now}", now)
    )

    return text, url


def parse_urls_to_facets(text: str) -> List[Dict[str, Any]]:
    facets: List[Dict[str, Any]] = []
    text_bytes = text.encode("utf-8")

    url_regex = rb"(https?:\/\/[^\s]+)"
    for m in re.finditer(url_regex, text_bytes):
        start = m.start(1)
        end = m.end(1)
        url = m.group(1).decode("utf-8")

        facets.append({
            "$type": "app.bsky.richtext.facet",
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{
                "$type": "app.bsky.richtext.facet#link",
                "uri": url,
            }],
        })

    return facets


def post_to_bluesky_external(
    handle: str,
    app_password: str,
    text: str,
    url: str,
    card_title: str,
    card_description: str,
) -> None:
    client = Client()
    client.login(handle, app_password)

    safe_text = (text or "").strip()
    if not safe_text:
        safe_text = f"{card_title}\n{url}"

    facets = parse_urls_to_facets(safe_text)

    client.send_post(
        safe_text,
        facets=facets if facets else None,
        embed={
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": url,
                "title": card_title,
                "description": card_description,
            },
        },
    )


def main() -> int:
    yt_api_key = must_env("YOUTUBE_API_KEY")
    yt_channel_id = must_env("YOUTUBE_CHANNEL_ID")
    bsky_handle = must_env("BLUESKY_HANDLE")
    bsky_app_password = must_env("BLUESKY_APP_PASSWORD")

    state_path = os.getenv("STATE_PATH", ".state/state.json")

    template_path = os.getenv("TEMPLATE_PATH", "template.txt")
    file_template = load_template(template_path)

    default_template = "{title}\n{url}\n@YouTubeより配信中！\n{now}"

    if file_template:
        template = file_template
    else:
        template = os.getenv("MESSAGE_TEMPLATE", default_template)

    state = load_state(state_path)
    last_notified = state.get("last_notified_video_id")

    try:
        live_video_id, title = youtube_get_live_video(yt_api_key, yt_channel_id)
    except Exception as e:
        print(f"ERROR: YouTube API call failed: {e}", file=sys.stderr)
        return 2

    if not live_video_id:
        print("No live broadcast detected.")
        return 0

    if live_video_id == last_notified:
        print(f"Already notified for video_id={live_video_id}")
        return 0

    title = title or "配信中"
    msg, url = build_message(template, live_video_id, title)

    try:
        post_to_bluesky_external(
            bsky_handle,
            bsky_app_password,
            msg,
            url,
            title,
            "YouTubeで配信中",
        )
    except Exception as e:
        print(f"ERROR: Bluesky post failed: {e}", file=sys.stderr)
        return 3

    state["last_notified_video_id"] = live_video_id
    save_state(state_path, state)

    print(f"Notified and saved state for video_id={live_video_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())