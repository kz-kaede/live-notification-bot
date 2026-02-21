import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import requests
from atproto import Client

# 警告を非表示（atproto/pydantic由来）
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

# YouTube Data API エンドポイント
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


# 環境変数を取得（未設定なら例外）
def must_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env: {name}")
    return v


# 前回通知した動画IDなどの状態を読み込む
def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


# 状態を保存（重複投稿防止）
def save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# 現在ライブ配信中の動画ID・タイトル・サムネURLを取得
def youtube_get_live_video(api_key: str, channel_id: str):
    params = {
        "key": api_key,
        "part": "snippet",
        "channelId": channel_id,
        "eventType": "live",   # 配信中の動画のみ取得
        "type": "video",
        "maxResults": 1,
        "order": "date",
    }
    r = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    items = data.get("items", [])
    if not items:
        return None, None, None

    item = items[0]
    video_id = (item.get("id") or {}).get("videoId")
    snippet = item.get("snippet") or {}

    title = snippet.get("title")
    thumbnails = snippet.get("thumbnails") or {}

    # 解像度の高い順にサムネイルURLを取得
    thumb_url = (
        (thumbnails.get("maxres") or {}).get("url")
        or (thumbnails.get("high") or {}).get("url")
        or (thumbnails.get("medium") or {}).get("url")
        or (thumbnails.get("default") or {}).get("url")
    )

    return video_id, title, thumb_url


# サムネイル画像をダウンロード（bytesで返す）
def download_image(url: str) -> bytes:
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.content

# テンプレートファイルを読み込む
def load_template(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None

# 投稿メッセージをテンプレートから生成
def build_message(template: str, video_id: str, title: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # 日本時間（JST）で現在時刻を取得
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).strftime("%Y-%m-%d %H:%M")

    # プレースホルダを置換
    return (
        template
        .replace("{url}", url)
        .replace("{video_id}", video_id)
        .replace("{title}", title or "")
        .replace("{now}", now)
    )


# Blueskyに投稿（画像があればサムネ付き）
def post_to_bluesky(handle: str, app_password: str, text: str, image_bytes: bytes | None) -> None:
    client = Client()
    client.login(handle, app_password)

    if image_bytes:
        # 画像アップロード
        upload = client.upload_blob(image_bytes)

        # 画像付き投稿
        client.send_post(
            text=text,
            embed={
                "$type": "app.bsky.embed.images",
                "images": [
                    {
                        "image": upload.blob,
                        "alt": "YouTube thumbnail",
                    }
                ],
            },
        )
    else:
        # テキストのみ投稿
        client.send_post(text=text)


# メイン処理
def main() -> int:
    # 必須環境変数の取得
    yt_api_key = must_env("YOUTUBE_API_KEY")
    yt_channel_id = must_env("YOUTUBE_CHANNEL_ID")
    bsky_handle = must_env("BLUESKY_HANDLE")
    bsky_app_password = must_env("BLUESKY_APP_PASSWORD")

    state_path = os.getenv("STATE_PATH", ".state/state.json")

    # 投稿テンプレート（template.txt を優先。無ければ環境変数、さらに無ければデフォルト）
    template_path = os.getenv("TEMPLATE_PATH", "template.txt")
    file_template = load_template(template_path)

    if file_template:
        template = file_template
    else:
        template = os.getenv(
            "MESSAGE_TEMPLATE",
            "「{title}」\n{url}（{now}）\n@YouTubeより配信中！"
        )

    # 前回通知状態を取得
    state = load_state(state_path)
    last_notified = state.get("last_notified_video_id")

    try:
        # YouTubeからライブ情報取得
        live_video_id, title, thumb_url = youtube_get_live_video(yt_api_key, yt_channel_id)
    except Exception as e:
        print(f"ERROR: YouTube API call failed: {e}", file=sys.stderr)
        return 2

    # 配信していない場合
    if not live_video_id:
        print("No live broadcast detected.")
        return 0

    # 既に通知済みならスキップ
    if live_video_id == last_notified:
        print(f"Already notified for video_id={live_video_id}")
        return 0

    # タイトルが取得できない場合のフォールバック
    title = title or "配信中"

    # 投稿メッセージ生成
    msg = build_message(template, live_video_id, title)

    try:
        image_bytes = None

        # サムネイルがあれば取得
        if thumb_url:
            image_bytes = download_image(thumb_url)

        # Blueskyへ投稿（画像が取れなければテキストのみ）
        post_to_bluesky(
            bsky_handle,
            bsky_app_password,
            msg,
            image_bytes
        )
    except Exception as e:
        print(f"ERROR: Bluesky post failed: {e}", file=sys.stderr)
        return 3

    # 通知済みIDを保存（重複防止）
    state["last_notified_video_id"] = live_video_id
    save_state(state_path, state)

    print(f"Notified and saved state for video_id={live_video_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())