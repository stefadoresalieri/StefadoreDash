from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

from yt_dlp import YoutubeDL


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
CACHE_PATH = ROOT / "live_cache.json"
SEED_PATH = ROOT / "seed_data.json"
CHANNEL_URL = "https://www.youtube.com/@stefadore/videos"
CHANNEL_HOME = "https://www.youtube.com/@stefadore"
CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCB051uh9yvyZuBJDY9_hyGQ")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
REFRESH_INTERVAL_SECONDS = 10 * 60
HOST = os.environ.get("STEFADORE_DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("STEFADORE_DASHBOARD_PORT", "8787"))

state_lock = threading.Lock()
refresh_lock = threading.Lock()

state = {
    "status": "booting",
    "channelUrl": CHANNEL_HOME,
    "lastUpdated": None,
    "nextRefreshAt": None,
    "refreshIntervalSeconds": REFRESH_INTERVAL_SECONDS,
    "error": None,
    "totals": {"videos": 0, "views": 0, "likes": 0, "comments": 0, "commentLikes": 0},
    "videos": [],
    "comments": [],
}


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def date_from_upload(raw: str | None) -> str:
    if not raw or len(raw) != 8:
        return ""
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def date_from_timestamp(timestamp) -> str:
    if not timestamp:
        return ""
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat()


def upload_date_from_iso(raw: str | None) -> str:
    if not raw:
        return ""
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed.strftime("%Y%m%d")


def compute_totals(videos: list[dict], comments: list[dict]) -> dict:
    return {
        "videos": len(videos),
        "views": sum(video.get("views") or 0 for video in videos),
        "likes": sum(video.get("likes") or 0 for video in videos),
        "comments": sum(video.get("comment_count") or 0 for video in videos),
        "commentLikes": sum(comment.get("likes") or 0 for comment in comments),
    }


def load_csv_bootstrap() -> dict:
    videos = []
    comments = []
    engagement = WORKSPACE / "stefadore_youtube_engagement_summary.csv"
    comment_file = WORKSPACE / "stefadore_youtube_comments.csv"

    if engagement.exists():
        with engagement.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                videos.append(
                    {
                        "upload_date": row.get("upload_date") or "",
                        "date": date_from_upload(row.get("upload_date")),
                        "id": row.get("id") or "",
                        "title": row.get("title") or "",
                        "views": int(row.get("views") or 0),
                        "likes": int(row.get("likes") or 0),
                        "comment_count": int(row.get("comment_count") or 0),
                        "url": row.get("url") or "",
                        "comment_likes": int(row.get("comment_likes") or 0),
                    }
                )

    if comment_file.exists():
        with comment_file.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                comments.append(
                    {
                        "video_title": row.get("video_title") or "",
                        "video_id": row.get("video_id") or "",
                        "video_url": row.get("video_url") or "",
                        "author": row.get("comment_author") or "",
                        "date": row.get("comment_date_utc") or "",
                        "likes": int(row.get("comment_likes") or 0),
                        "parent": row.get("parent") or "root",
                        "text": row.get("comment_text") or "",
                    }
                )

    return {
        "status": "ready",
        "channelUrl": CHANNEL_HOME,
        "lastUpdated": None,
        "nextRefreshAt": None,
        "refreshIntervalSeconds": REFRESH_INTERVAL_SECONDS,
        "error": None,
        "totals": compute_totals(videos, comments),
        "videos": sorted(videos, key=lambda item: item.get("upload_date") or "", reverse=True),
        "comments": comments,
    }


def load_initial_state() -> None:
    global state
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as handle:
                cached = json.load(handle)
            cached["status"] = "ready"
            cached["nextRefreshAt"] = None
            with state_lock:
                state = cached
            return
        except Exception:
            pass

    if SEED_PATH.exists():
        try:
            with SEED_PATH.open("r", encoding="utf-8") as handle:
                seeded = json.load(handle)
            seeded["status"] = "ready"
            seeded["nextRefreshAt"] = None
            seeded["error"] = "Showing the bundled snapshot until a live refresh succeeds."
            with state_lock:
                state = seeded
            return
        except Exception:
            pass

    bootstrap = load_csv_bootstrap()
    with state_lock:
        state = bootstrap


def api_get_json(path: str, params: dict) -> dict:
    if not YOUTUBE_API_KEY:
        raise RuntimeError("Set YOUTUBE_API_KEY in Render to enable live YouTube refreshes.")

    query = urlencode({**params, "key": YOUTUBE_API_KEY})
    request = urllib.request.Request(f"https://www.googleapis.com/youtube/v3/{path}?{query}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def collect_api_comments(video_id: str, video_title: str) -> list[dict]:
    comments = []
    page_token = None
    while True:
        params = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": 100,
            "textFormat": "plainText",
            "order": "time",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = api_get_json("commentThreads", params)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return comments
            raise

        for item in data.get("items", []):
            top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            comments.append(
                {
                    "video_title": video_title,
                    "video_id": video_id,
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "author": top.get("authorDisplayName") or "",
                    "date": (top.get("publishedAt") or "")[:10],
                    "likes": int(top.get("likeCount") or 0),
                    "parent": "root",
                    "text": top.get("textDisplay") or top.get("textOriginal") or "",
                }
            )

            for reply in item.get("replies", {}).get("comments", []):
                snippet = reply.get("snippet", {})
                comments.append(
                    {
                        "video_title": video_title,
                        "video_id": video_id,
                        "video_url": f"https://www.youtube.com/watch?v={video_id}",
                        "author": snippet.get("authorDisplayName") or "",
                        "date": (snippet.get("publishedAt") or "")[:10],
                        "likes": int(snippet.get("likeCount") or 0),
                        "parent": item.get("id") or "root",
                        "text": snippet.get("textDisplay") or snippet.get("textOriginal") or "",
                    }
                )

        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return comments


def collect_api_data() -> dict:
    channel_data = api_get_json("channels", {"part": "contentDetails", "id": CHANNEL_ID})
    channel_items = channel_data.get("items") or []
    if not channel_items:
        raise RuntimeError(f"YouTube API could not find channel {CHANNEL_ID}.")

    uploads_playlist = channel_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    video_ids = []
    page_token = None
    while True:
        params = {"part": "contentDetails", "playlistId": uploads_playlist, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        page = api_get_json("playlistItems", params)
        video_ids.extend(
            item.get("contentDetails", {}).get("videoId")
            for item in page.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        )
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    videos = []
    comments = []
    for offset in range(0, len(video_ids), 50):
        details = api_get_json(
            "videos",
            {"part": "snippet,statistics", "id": ",".join(video_ids[offset : offset + 50])},
        )
        for item in details.get("items", []):
            video_id = item["id"]
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            title = snippet.get("title") or ""
            video_comments = collect_api_comments(video_id, title)
            comments.extend(video_comments)
            upload_date = upload_date_from_iso(snippet.get("publishedAt"))
            videos.append(
                {
                    "upload_date": upload_date,
                    "date": date_from_upload(upload_date),
                    "id": video_id,
                    "title": title,
                    "views": int(stats.get("viewCount") or 0),
                    "likes": int(stats.get("likeCount") or 0),
                    "comment_count": int(stats.get("commentCount") or len(video_comments)),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "comment_likes": sum(comment["likes"] for comment in video_comments),
                }
            )

    videos.sort(key=lambda item: item.get("upload_date") or "", reverse=True)
    comments.sort(key=lambda item: (item.get("video_title") or "", item.get("parent") != "root", item.get("author") or ""))

    return build_refreshed_payload(videos, comments, error=None)


def fetch_like_count(video_id: str) -> int | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        html = urllib.request.urlopen(request, timeout=20).read().decode("utf-8", "replace")
    except Exception:
        return None

    match = re.search(r'"likeCount"\s*:\s*"?([0-9,]+)"?', html)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def build_refreshed_payload(videos: list[dict], comments: list[dict], error: str | None) -> dict:
    return {
        "status": "ready",
        "channelUrl": CHANNEL_HOME,
        "lastUpdated": iso_now(),
        "nextRefreshAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=REFRESH_INTERVAL_SECONDS)).isoformat(),
        "refreshIntervalSeconds": REFRESH_INTERVAL_SECONDS,
        "error": error,
        "totals": compute_totals(videos, comments),
        "videos": videos,
        "comments": comments,
    }


def collect_scraped_data() -> dict:
    flat_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    video_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "getcomments": True,
        "ignoreerrors": True,
    }

    with YoutubeDL(flat_options) as ydl:
        playlist = ydl.extract_info(CHANNEL_URL, download=False)

    entries = [entry for entry in (playlist.get("entries") or []) if entry and entry.get("id")]
    videos = []
    comments = []

    with YoutubeDL(video_options) as ydl:
        for entry in entries:
            video_id = entry["id"]
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if not info:
                continue

            like_count = info.get("like_count")
            if like_count is None:
                like_count = fetch_like_count(video_id) or 0

            extracted_comments = info.get("comments") or []
            video_comments = []
            for comment in extracted_comments:
                clean_comment = {
                    "video_title": info.get("title") or "",
                    "video_id": video_id,
                    "video_url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                    "author": comment.get("author") or "",
                    "date": date_from_timestamp(comment.get("timestamp")),
                    "likes": int(comment.get("like_count") or 0),
                    "parent": comment.get("parent") or "root",
                    "text": (comment.get("text") or "").replace("\r", " ").strip(),
                }
                comments.append(clean_comment)
                video_comments.append(clean_comment)

            upload_date = info.get("upload_date") or entry.get("upload_date") or ""
            videos.append(
                {
                    "upload_date": upload_date,
                    "date": date_from_upload(upload_date),
                    "id": video_id,
                    "title": info.get("title") or entry.get("title") or "",
                    "views": int(info.get("view_count") or 0),
                    "likes": int(like_count or 0),
                    "comment_count": int(info.get("comment_count") or len(video_comments)),
                    "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                    "comment_likes": sum(comment["likes"] for comment in video_comments),
                }
            )

    videos.sort(key=lambda item: item.get("upload_date") or "", reverse=True)
    comments.sort(key=lambda item: (item.get("video_title") or "", item.get("parent") != "root", item.get("author") or ""))

    if not videos:
        raise RuntimeError("YouTube blocked the public scraper. Set YOUTUBE_API_KEY in Render for reliable hosted refreshes.")

    refreshed = build_refreshed_payload(videos, comments, error=None)

    return refreshed


def collect_live_data() -> dict:
    if YOUTUBE_API_KEY:
        refreshed = collect_api_data()
    else:
        refreshed = collect_scraped_data()

    with CACHE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(refreshed, handle, ensure_ascii=False, indent=2)
    return refreshed


def refresh_data(force: bool = False) -> bool:
    if not refresh_lock.acquire(blocking=False):
        return False

    def worker() -> None:
        global state
        try:
            with state_lock:
                state["status"] = "refreshing"
                state["error"] = None
            refreshed = collect_live_data()
            with state_lock:
                state = refreshed
        except Exception as exc:
            with state_lock:
                state["status"] = "ready" if state.get("videos") else "error"
                state["error"] = str(exc)
                state["nextRefreshAt"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=REFRESH_INTERVAL_SECONDS)).isoformat()
        finally:
            refresh_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return True


def refresh_scheduler() -> None:
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        refresh_data()


def local_ip_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except Exception:
        return "localhost"


class LiveDashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_file(ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/data":
            with state_lock:
                payload = json.loads(json.dumps(state))
            self.send_json(payload)
            return
        if parsed.path == "/api/refresh":
            started = refresh_data(force=True)
            with state_lock:
                payload = json.loads(json.dumps(state))
            payload["refreshStarted"] = started
            self.send_json(payload, 202 if started else 200)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            started = refresh_data(force=True)
            with state_lock:
                payload = json.loads(json.dumps(state))
            payload["refreshStarted"] = started
            self.send_json(payload, 202 if started else 200)
            return
        self.send_error(404)


def main() -> None:
    load_initial_state()
    refresh_data(force=True)
    threading.Thread(target=refresh_scheduler, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), LiveDashboardHandler)
    network_host = local_ip_address()
    print(f"Live dashboard running on this computer: http://127.0.0.1:{PORT}")
    print(f"Live dashboard for your phone: http://{network_host}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
