from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from yt_dlp import YoutubeDL


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
CACHE_PATH = ROOT / "live_cache.json"
SEED_PATH = ROOT / "seed_data.json"
CHANNEL_URL = "https://www.youtube.com/@stefadore/videos"
CHANNEL_HOME = "https://www.youtube.com/@stefadore"
CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCB051uh9yvyZuBJDY9_hyGQ")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
ALLOW_PUBLIC_YOUTUBE_SCRAPER = os.environ.get("ALLOW_PUBLIC_YOUTUBE_SCRAPER", "false").lower() == "true"
REFRESH_INTERVAL_SECONDS = 10 * 60
HOST = os.environ.get("STEFADORE_DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("STEFADORE_DASHBOARD_PORT", "8787"))
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "stefadore")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
DASHBOARD_SESSION_SECRET = os.environ.get("DASHBOARD_SESSION_SECRET") or DASHBOARD_PASSWORD or secrets.token_urlsafe(32)
AUTH_COOKIE_NAME = "stefadore_dashboard_session"

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


def duration_seconds_from_iso(raw: str | None) -> int:
    if not raw:
        return 0
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", raw)
    if not match:
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def video_type_from_duration(duration_seconds: int | None) -> str:
    return "short" if duration_seconds and duration_seconds <= 60 else "regular"


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
                        "duration_seconds": int(row.get("duration_seconds") or 0),
                        "video_type": row.get("video_type") or "regular",
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
    seeded = None
    if SEED_PATH.exists():
        try:
            with SEED_PATH.open("r", encoding="utf-8") as handle:
                seeded = json.load(handle)
            seeded["status"] = "ready"
            seeded["nextRefreshAt"] = None
            seeded["error"] = "Showing the bundled snapshot until a live refresh succeeds."
        except Exception:
            seeded = None

    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as handle:
                cached = json.load(handle)
            if seeded and len(seeded.get("videos") or []) > len(cached.get("videos") or []):
                with state_lock:
                    state = seeded
                return
            cached["status"] = "ready"
            cached["nextRefreshAt"] = None
            with state_lock:
                state = cached
            return
        except Exception:
            pass

    if seeded:
        with state_lock:
            state = seeded
        return

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
            {"part": "snippet,statistics,contentDetails", "id": ",".join(video_ids[offset : offset + 50])},
        )
        for item in details.get("items", []):
            video_id = item["id"]
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content_details = item.get("contentDetails", {})
            title = snippet.get("title") or ""
            video_comments = collect_api_comments(video_id, title)
            comments.extend(video_comments)
            upload_date = upload_date_from_iso(snippet.get("publishedAt"))
            duration_seconds = duration_seconds_from_iso(content_details.get("duration"))
            videos.append(
                {
                    "upload_date": upload_date,
                    "date": date_from_upload(upload_date),
                    "id": video_id,
                    "title": title,
                    "duration_seconds": duration_seconds,
                    "video_type": video_type_from_duration(duration_seconds),
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
            duration_seconds = int(info.get("duration") or entry.get("duration") or 0)
            videos.append(
                {
                    "upload_date": upload_date,
                    "date": date_from_upload(upload_date),
                    "id": video_id,
                    "title": info.get("title") or entry.get("title") or "",
                    "duration_seconds": duration_seconds,
                    "video_type": video_type_from_duration(duration_seconds),
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
    elif ALLOW_PUBLIC_YOUTUBE_SCRAPER:
        refreshed = collect_scraped_data()
    else:
        raise RuntimeError("Live refresh is paused until YOUTUBE_API_KEY is added in Render.")

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


def auth_enabled() -> bool:
    return bool(DASHBOARD_PASSWORD)


def make_session_token() -> str:
    signature = hmac.new(
        DASHBOARD_SESSION_SECRET.encode("utf-8"),
        DASHBOARD_USERNAME.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{DASHBOARD_USERNAME}:{signature}"


def secure_cookie_suffix(handler: BaseHTTPRequestHandler) -> str:
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "")
    is_https = forwarded_proto.lower() == "https"
    return "; Secure" if is_https else ""


def login_page(error: str = "") -> bytes:
    escaped_error = html.escape(error)
    error_html = f'<p class="error">{escaped_error}</p>' if escaped_error else ""
    username = html.escape(DASHBOARD_USERNAME)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stefadore Dashboard Login</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #2c1726;
      --muted: #7b6473;
      --line: #efd1dd;
      --panel: #fffafd;
      --page: #fff0f6;
      --red: #d63384;
      --teal: #a83279;
      --shadow: 0 10px 28px rgba(126, 38, 88, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 24px;
      background: var(--page);
      color: var(--ink);
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
    }}
    main {{
      width: min(420px, 100%);
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.05; }}
    p {{ margin: 0 0 22px; color: var(--muted); font-size: 14px; }}
    label {{ display: grid; gap: 7px; margin-bottom: 14px; font-size: 13px; font-weight: 800; }}
    input {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 12px;
      color: var(--ink);
      font: inherit;
    }}
    button {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--red);
      border-radius: 8px;
      background: var(--red);
      color: white;
      font: inherit;
      font-weight: 850;
      cursor: pointer;
    }}
    .error {{
      margin: 0 0 14px;
      padding: 10px 12px;
      border: 1px solid #f1b6cc;
      border-radius: 8px;
      background: #fff5fa;
      color: #8f1f51;
      font-weight: 750;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Stefadore Dashboard</h1>
    <p>Sign in to view the dashboard data.</p>
    {error_html}
    <form method="post" action="/login">
      <label>Username
        <input name="username" value="{username}" autocomplete="username" required>
      </label>
      <label>Password
        <input name="password" type="password" autocomplete="current-password" required autofocus>
      </label>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>""".encode("utf-8")


class LiveDashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def cookie_value(self, name: str) -> str | None:
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def is_authenticated(self) -> bool:
        if not auth_enabled():
            return True
        return hmac.compare_digest(self.cookie_value(AUTH_COOKIE_NAME) or "", make_session_token())

    def redirect(self, location: str, status: int = 303) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def require_auth(self, wants_json: bool = False) -> bool:
        if self.is_authenticated():
            return True
        if wants_json:
            self.send_json({"error": "Sign in required."}, 401)
        else:
            self.redirect("/login")
        return False

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_login_page(self, error: str = "", status: int = 200) -> None:
        data = login_page(error)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def set_auth_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{AUTH_COOKIE_NAME}={make_session_token()}; Path=/; HttpOnly; SameSite=Lax{secure_cookie_suffix(self)}",
        )

    def clear_auth_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{AUTH_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure_cookie_suffix(self)}",
        )

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
        if parsed.path == "/login":
            if self.is_authenticated():
                self.redirect("/")
                return
            self.send_login_page()
            return
        if parsed.path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.clear_auth_cookie()
            self.end_headers()
            return
        if parsed.path in ("/", "/index.html"):
            if not self.require_auth():
                return
            self.send_file(ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/architecture":
            if not self.require_auth():
                return
            self.send_file(ROOT / "architecture.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/data":
            if not self.require_auth(wants_json=True):
                return
            with state_lock:
                payload = json.loads(json.dumps(state))
            self.send_json(payload)
            return
        if parsed.path == "/api/refresh":
            if not self.require_auth(wants_json=True):
                return
            started = refresh_data(force=True)
            with state_lock:
                payload = json.loads(json.dumps(state))
            payload["refreshStarted"] = started
            self.send_json(payload, 202 if started else 200)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            username = (form.get("username") or [""])[0]
            password = (form.get("password") or [""])[0]
            username_ok = hmac.compare_digest(username, DASHBOARD_USERNAME)
            password_ok = bool(DASHBOARD_PASSWORD) and hmac.compare_digest(password, DASHBOARD_PASSWORD)
            if username_ok and password_ok:
                self.send_response(303)
                self.send_header("Location", "/")
                self.set_auth_cookie()
                self.end_headers()
                return
            self.send_login_page("That username or password was not recognised.", 401)
            return
        if parsed.path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.clear_auth_cookie()
            self.end_headers()
            return
        if parsed.path == "/api/refresh":
            if not self.require_auth(wants_json=True):
                return
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
