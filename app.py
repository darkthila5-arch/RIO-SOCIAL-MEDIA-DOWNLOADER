"""Rio Downloader - Flask backend (YouTube + Instagram + Facebook + TikTok + Pinterest in one site).

The pasted link decides which platform is used (see detect_platform).

Flow
    /api/info     -> look the video up with yt-dlp (only reads the details, downloads nothing)
    /api/prepare  -> start a download. Answers with either
                       {"file": "/dl/<token>"}  ready now (cover image, details TXT, plain Instagram/Facebook files), or
                       {"job": "<id>"}          a video / MP3 to prepare, poll it at /api/job/<id>
    /dl/<token>   -> streams a ready direct download
    /file/<token> -> serves a finished job result (MP4, MP3)

YouTube keeps picture and sound in separate streams and throttles plain downloads, so YouTube videos are
always a job: yt-dlp downloads both streams and joins them with ffmpeg (no re-encoding).
Instagram, Facebook and TikTok usually serve one ready MP4, so those stream straight through, unless a quality is
picture-only (then the server joins picture + sound) or the Rio watermark is requested.

Every download goes through /api/prepare first, so a failure always becomes a readable message
in the page instead of a broken file in the browser.
"""
import html as htmllib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

try:
    import yt_dlp
except ImportError:  # the server then answers with a clear message
    yt_dlp = None
try:
    from curl_cffi import requests as cffi  # Chrome-like TLS fingerprint (Facebook page reader)
except ImportError:
    cffi = None
try:
    import imageio_ffmpeg  # ships a ready-made ffmpeg binary
except ImportError:
    imageio_ffmpeg = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # API bodies are tiny; refuse anything big


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# ---------------------------------------------------------------------------
# Config (all optional, set in Render > Environment)
# ---------------------------------------------------------------------------
RATE_LIMIT = _int_env("RATE_LIMIT", 12)                  # requests per window, per visitor
RATE_WINDOW = _int_env("RATE_WINDOW", 60)                # seconds
GLOBAL_RATE_LIMIT = _int_env("GLOBAL_RATE_LIMIT", 240)   # safety net for the whole site, can't be spoofed
PROXY_HOPS = _int_env("PROXY_HOPS", 2)                   # proxies in front of the app (0 = ignore X-Forwarded-For)
CACHE_TTL = _int_env("CACHE_TTL", 600)
PROXY_URL = os.environ.get("PROXY_URL") or None
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"     # show real errors in the UI + enable /api/debug/ip
MAX_HEIGHT = _int_env("MAX_HEIGHT", 1080)                # tallest quality offered (above 1080 the file is an MKV)
MAX_DURATION = _int_env("MAX_DURATION", 1800)            # longest YouTube video accepted, in seconds
CACHE_TTL = _int_env("CACHE_TTL", 600)
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
RESOLVE_BUDGET = _int_env("RESOLVE_BUDGET", 50)          # seconds spent on all providers together (Instagram / Facebook / TikTok)
TIKTOK_PROVIDERS = [p.strip() for p in (os.environ.get("TIKTOK_PROVIDERS") or os.environ.get("PROVIDERS") or "own,tikwm,ytdlp").split(",") if p.strip()]
PARALLEL_JOBS = _int_env("PARALLEL_JOBS", 2)             # downloads running at once (watermark encoding is always one at a time)

# Watermark = the ghost logo in static/watermark.png (needs ffmpeg only)
WATERMARK_ENABLED = os.environ.get("WATERMARK", "1") != "0"
WATERMARK_IMAGE = os.environ.get("WATERMARK_IMAGE", os.path.join(BASE_DIR, "static", "watermark.png"))
WATERMARK_SIZE = _float_env("WATERMARK_SIZE", 0.24)      # logo width as a share of the video width
WATERMARK_OPACITY = min(1.0, max(0.1, _float_env("WATERMARK_OPACITY", 0.92)))
WATERMARK_POSITIONS = ("br", "bl", "tr", "tl")           # bottom-right, bottom-left, top-right, top-left
WATERMARK_DEFAULT_POS = os.environ.get("WATERMARK_POSITION", "br")
if WATERMARK_DEFAULT_POS not in WATERMARK_POSITIONS:
    WATERMARK_DEFAULT_POS = "br"

FFMPEG_PRESET = os.environ.get("FFMPEG_PRESET", "ultrafast")  # faster = bigger file
FFMPEG_TIMEOUT = _int_env("FFMPEG_TIMEOUT", 240)
MAX_SOURCE_MB = _int_env("MAX_SOURCE_MB", 250)
MAX_ACTIVE_JOBS = _int_env("MAX_ACTIVE_JOBS", 5)
FILE_TTL = 600     # how long a finished ffmpeg file stays downloadable
TICKET_TTL = 900   # how long a direct-download link stays valid
JOB_TTL = 900

# Live download counter. Set the two Upstash variables to keep the number forever;
# without them it is saved to a temp file that Render wipes on restart.
UPSTASH_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
COUNTER_KEY = os.environ.get("COUNTER_KEY", "rio_downloads")
COUNTER_FILE = os.environ.get("COUNTER_FILE", os.path.join(tempfile.gettempdir(), "rio_counter.json"))

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
# A link ends at whitespace, quotes, brackets or CJK / full-width punctuation.
URL_RE = re.compile(r"https?://[^\s<>\"'`“”‘’「」『』。、，！？）】]+")
TRAILING = ".,;:!?)]}>*_"
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


class ResolveError(Exception):
    pass


class UserError(ResolveError):
    """A problem with this particular video; the message is written for the visitor."""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def json_body():
    """The request body as a dict, whatever the client sent."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_field(data, key, limit=2000):
    value = data.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


BARE_RE = re.compile(r"(?<![\w@/.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s<>\"'`“”‘’「」『』。、，！？）】]*)?")


def extract_url(text):
    """Accept a bare link or the whole share text and pull the link out.
    A link pasted without https:// (for example 'fb.watch/abc' or 'www.facebook.com/reel/1') is accepted too."""
    text = text if isinstance(text, str) else ""
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(TRAILING)
    for bare in BARE_RE.finditer(text):
        candidate = "https://" + bare.group(0).rstrip(TRAILING)
        if detect_platform(candidate):
            return candidate
    return ""


PLATFORMS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "facebook": ("facebook.com", "fb.watch", "fb.com", "fb.me"),
    "tiktok": ("tiktok.com",),
    "pinterest": ("pinterest.com", "pin.it"),  # regional sites (pinterest.co.uk, .de, ...) are matched by PINTEREST_HOST_RE
}
PINTEREST_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*pinterest\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$")
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "pinterest": "Pinterest"}
LINK_ERROR = "That doesn't look like a YouTube, Instagram, Facebook, TikTok or Pinterest link."


def detect_platform(url):
    """'youtube', 'instagram', 'facebook', 'tiktok', 'pinterest' or None. Only real hosts count, so the server can't be used as an open proxy."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    for name, hosts in PLATFORMS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    if PINTEREST_HOST_RE.match(host):
        return "pinterest"
    return None


def error(message, status, detail=None):
    body = {"error": message}
    if DEBUG_ERRORS and detail:
        body["detail"] = str(detail)[:300]
    resp = jsonify(body)
    resp.status_code = status
    return resp


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_date(epoch=None, yyyymmdd=None):
    try:
        if epoch:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
        if yyyymmdd and len(str(yyyymmdd)) == 8:
            s = str(yyyymmdd)
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    except (ValueError, OSError, OverflowError):
        pass
    return None


def safe_name(value, fallback="video"):
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
    return cleaned[:40] or fallback


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_hits = defaultdict(deque)
_global_hits = deque()
_rate_lock = threading.Lock()
MAX_TRACKED_IPS = 5000


def client_ip():
    """The visitor's IP. X-Forwarded-For can be forged by the client, so only the
    entry added by our own proxies (counted from the right) is trusted."""
    parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if PROXY_HOPS > 0 and parts:
        return parts[-PROXY_HOPS] if len(parts) >= PROXY_HOPS else parts[0]
    return request.remote_addr or "unknown"


def _trim(q, now):
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()


def rate_limited():
    now = time.time()
    with _rate_lock:
        _trim(_global_hits, now)
        if len(_global_hits) >= GLOBAL_RATE_LIMIT:
            return True
        q = _hits[client_ip()]
        _trim(q, now)
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        _global_hits.append(now)
        if len(_hits) > MAX_TRACKED_IPS:  # keep memory bounded, even under spoofed addresses
            for ip in [ip for ip, d in _hits.items() if not d or now - d[-1] > RATE_WINDOW]:
                _hits.pop(ip, None)
            if len(_hits) > MAX_TRACKED_IPS:
                _hits.clear()
    return False


# ---------------------------------------------------------------------------
# Cache (links expire, so keep them only for a few minutes)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        _cache.pop(key, None)
    return None


def cache_set(key, value):
    with _cache_lock:
        if len(_cache) > 200:
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(k, None)
            if len(_cache) > 200:
                _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, value)


# ---------------------------------------------------------------------------
# yt-dlp access (it does all the talking to the sites)
# ---------------------------------------------------------------------------
def _read_cookies(env_text, env_path, default_path):
    """Optional logged-in cookies (Netscape cookies.txt). YouTube and Instagram often refuse data-centre IPs;
    cookies from a throw-away account make that much less likely."""
    text = os.environ.get(env_text) or ""
    path = os.environ.get(env_path) or default_path   # default is the Render "Secret Files" location
    if not text and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            app.logger.warning("could not read the cookies file %s: %s", path, exc)
    text = text.strip()
    if not text:
        return ""
    if "\\n" in text and "\n" not in text:  # pasted into a one-line variable
        text = text.replace("\\n", "\n")
    if not text.startswith(("# Netscape", "# HTTP Cookie File")):
        text = "# Netscape HTTP Cookie File\n" + text
    return text + "\n"


COOKIES_TEXT = {
    "youtube": _read_cookies("YOUTUBE_COOKIES", "COOKIES_FILE", "/etc/secrets/cookies.txt"),
    "instagram": _read_cookies("INSTAGRAM_COOKIES", "INSTAGRAM_COOKIES_FILE", "/etc/secrets/instagram_cookies.txt"),
    "facebook": "",
}
_ffdir = None


def ffmpeg_dir():
    """Folder where yt-dlp should look for ffmpeg (None = it finds a system ffmpeg itself).
    imageio-ffmpeg's binary has an odd file name, so it is linked as plain 'ffmpeg'."""
    global _ffdir
    if _ffdir is None:
        _ffdir = ""
        exe = ffmpeg_path()
        if exe and not shutil.which("ffmpeg"):
            folder = os.path.join(tempfile.gettempdir(), "rio_ffbin")
            try:
                os.makedirs(folder, exist_ok=True)
                link = os.path.join(folder, "ffmpeg")
                if not os.path.exists(link):
                    os.symlink(exe, link)
                _ffdir = folder
            except OSError:
                _ffdir = ""
    return _ffdir or None


@contextmanager
def ydl_session(platform, **extra):
    """A yt-dlp instance with our usual settings (proxy, cookies for that platform, JS runtime, ffmpeg)."""
    if yt_dlp is None:
        raise UserError("yt-dlp is not installed on this server.")
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
            "socket_timeout": 20, "retries": 3, "fragment_retries": 5, "restrictfilenames": True}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:  # YouTube's signature puzzle needs a JavaScript runtime
        opts["js_runtimes"] = runtimes
    folder = ffmpeg_dir()
    if folder:
        opts["ffmpeg_location"] = folder
    cookie_copy = None
    cookies = COOKIES_TEXT.get(platform) or ""
    if cookies:  # yt-dlp rewrites its cookie file, so every session gets its own copy
        fd, cookie_copy = tempfile.mkstemp(prefix="rio_ck_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(cookies)
        opts["cookiefile"] = cookie_copy
    opts.update(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if cookie_copy:
            try:
                os.remove(cookie_copy)
            except OSError:
                pass


BOT_NEEDLES = ("not a bot", "confirm you", "sign in to")
ERROR_RULES = (  # (text to look for in yt-dlp's message, what to tell the visitor) - first match wins; {name} = the site
    (("private video",), "This video is private."),
    (("members-only", "members only", "join this channel"), "This video is only for channel members."),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "This video is age-restricted, so it can't be saved."),
    (BOT_NEEDLES, "{name} is blocking this server right now (it wants to check that it isn't a bot), so the "
                  "video can't be read. Try again later."),
    (("not available in your country", "blocked it in your country", "geo restrict", "geo-restrict"),
     "This video isn't available in the country the server is in."),
    (("copyright",), "This video was blocked for copyright reasons."),
    (("premieres in", "live event will begin", "will begin in"), "This is an upcoming live stream or premiere."),
    (("login required", "rate-limit reached", "rate limit reached", "empty media response", "cannot parse data",
      "log in to"),
     "{name} didn't let the server read this one. It may be private, or {name} is limiting this server right now."),
    (("video unavailable", "has been removed", "been terminated", "no longer available", "does not exist"),
     "This video is unavailable or was removed."),
    (("requested format is not available",), "{name} didn't offer a downloadable version of this video."),
)


def is_bot_error(exc):
    text = str(exc).lower()
    return any(n in text for n in BOT_NEEDLES)


def explain(exc, default, platform=None):
    """Turn a yt-dlp error into one readable sentence."""
    if isinstance(exc, UserError):
        return str(exc)
    name = PLATFORM_NAMES.get(platform, "The site")
    text = str(exc).lower()
    for needles, message in ERROR_RULES:
        if any(n in text for n in needles):
            return message.format(name=name)
    return default


def _size(f):
    return to_int(f.get("filesize") or f.get("filesize_approx")) or 0


# If YouTube answers "not a bot" to the normal lookup, one more try with other player clients sometimes works
# without cookies. The settings that worked are kept in info["ytdl_extra"] so the download uses them too.
ALT_YT_CLIENTS = {"extractor_args": {"youtube": {"player_client": ["tv", "mweb", "web_safari"]}}}


def from_youtube(url):
    try:
        return _youtube_lookup(url, {})
    except Exception as exc:  # noqa: BLE001
        if not is_bot_error(exc):
            raise
        app.logger.info("YouTube bot check on the normal lookup, retrying with other player clients")
        try:
            return _youtube_lookup(url, ALT_YT_CLIENTS)
        except Exception:  # noqa: BLE001
            raise exc


def _youtube_lookup(url, extra):
    with ydl_session("youtube", **extra) as ydl:
        raw = ydl.extract_info(url, download=False)
    if not raw:
        raise UserError("Couldn't read this link.")
    if raw.get("_type") in ("playlist", "multi_video"):
        raise UserError("That link is a playlist or a channel. Paste the link of a single video.")
    if raw.get("is_live") or raw.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise UserError("Live streams and premieres can't be saved. Try again after it has ended.")
    duration = to_int(raw.get("duration"))
    if duration and duration > MAX_DURATION:
        raise UserError(f"This video is too long. The limit is {MAX_DURATION // 60} minutes.")

    formats = raw.get("formats") or []
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0) if audio else None
    audio_size = _size(best_audio) if best_audio else 0

    by_height = {}
    for f in formats:
        h = to_int(f.get("height"))
        if h and f.get("vcodec") not in (None, "none"):
            by_height.setdefault(h, []).append(f)
    limit = MAX_SOURCE_MB * 1024 * 1024
    rows = []
    for h in sorted((h for h in by_height if h <= MAX_HEIGHT), reverse=True):
        biggest = max(_size(f) for f in by_height[h])
        rows.append((h, biggest + audio_size if biggest else None))
    fits = [r for r in rows if r[1] is None or r[1] <= limit]  # hide qualities that are over the size limit
    rows = (fits or rows[-1:])[:6]
    if not rows:
        raise UserError("YouTube didn't offer a downloadable video for this link. It may need a login.")

    handle = str(raw.get("uploader_id") or "").lstrip("@")
    return {
        "platform": "youtube",
        "ytdl_extra": extra,
        "id": str(raw.get("id") or "video"),
        "title": (raw.get("title") or "YouTube video").strip(),
        "author": raw.get("channel") or raw.get("uploader") or "",
        "handle": handle if handle and not handle.startswith("UC") else "",
        "thumbnail": raw.get("thumbnail"),
        "duration": duration,
        "views": to_int(raw.get("view_count")),
        "likes": to_int(raw.get("like_count")),
        "comments": to_int(raw.get("comment_count")),
        "shares": None,
        "created": iso_date(epoch=raw.get("timestamp"), yyyymmdd=raw.get("upload_date")),
        "tags": [str(x) for x in (raw.get("tags") or [])][:15],
        "formats": [{"id": f"h{h}", "label": f"{h}p", "kind": "video", "size": size, "height": h}
                    for h, size in rows],
    }


# ---------------------------------------------------------------------------
# Instagram + Facebook (yt-dlp first; Facebook also has our own page reader as a second try)
# ---------------------------------------------------------------------------
def _is_http(f):
    return bool(f.get("url")) and (f.get("protocol") or "https") in ("http", "https")


def _headers_of(ydl, info, f):
    headers = dict(f.get("http_headers") or info.get("http_headers") or {})
    cookie = ydl.cookiejar.get_cookie_header(f["url"])
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _video_label(f):
    if f.get("height"):
        return f"{f['height']}p"
    fid = str(f.get("format_id") or "").lower()
    return {"hd": "HD", "sd": "Standard"}.get(fid) or (str(f.get("format_note") or "").strip() or "Video")


def _kinds(formats):
    """e.g. 'video x3, audio x1' - for the logs."""
    count = {}
    for f in formats or []:
        count[f.get("kind", "?")] = count.get(f.get("kind", "?"), 0) + 1
    return ", ".join(f"{k} x{v}" for k, v in sorted(count.items())) or "none"


def from_social(url, platform):
    with ydl_session(platform, skip_download=True) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") in ("playlist", "multi_video"):  # a link to a post with several items
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise ResolveError("ytdlp: no video found in this post")
            info = entries[0]

        raw = info.get("formats") or [info]
        if platform == "pinterest":
            app.logger.info("pinterest yt-dlp saw %d formats: %s", len(raw), [
                (f.get("format_id"), f.get("protocol"), f.get("ext"), f.get("height"), f.get("vcodec")) for f in raw][:12])
        usable = [f for f in raw if _is_http(f)]
        audio_only = [f for f in usable if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        audio_only.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
        best_audio = audio_only[0] if audio_only else None

        videos = [f for f in usable if f.get("vcodec") != "none"]
        # best picture first; at the same quality prefer a file that already has sound (no joining needed)
        videos.sort(key=lambda f: (f.get("height") or 0, f.get("acodec") != "none", f.get("tbr") or 0), reverse=True)

        formats, seen = [], set()
        for f in videos:
            silent = f.get("acodec") == "none"  # picture only (DASH): needs the separate sound stream
            if silent and not best_audio:
                continue
            label = _video_label(f)
            if label in seen:
                continue
            seen.add(label)
            entry = {
                "id": f"y{len(formats)}", "label": label, "kind": "video",
                "size": to_int(f.get("filesize") or f.get("filesize_approx")),
                "url": f["url"], "headers": _headers_of(ydl, info, f), "proxy": bool(PROXY_URL),
            }
            if silent:
                entry.update(audio_url=best_audio["url"], audio_headers=_headers_of(ydl, info, best_audio))
            formats.append(entry)
            if len(formats) >= 5:
                break

        if platform == "pinterest" and not formats:
            # Pinterest often lists a video only as HLS (.m3u8); a plain MP4 of it usually sits next to it,
            # and if it does not, ffmpeg saves the HLS playlist itself as an MP4
            hls_links = [f for f in raw if ".m3u8" in str(f.get("url") or "").split("?")[0].lower()]
            hls_links.sort(key=lambda f: f.get("height") or 0, reverse=True)
            for f in hls_links:
                hit = _pin_first_mp4(str(f["url"]))
                if hit:
                    formats.append({"id": "y0", "label": f"{f.get('height') or hit[0] or 720}p", "kind": "video", "size": None,
                                    "url": hit[1], "headers": {"User-Agent": UA}, "proxy": bool(PROXY_URL)})
                    break
            if not formats and hls_links:
                f = hls_links[0]
                formats.append({"id": "y0", "label": f"{f['height']}p" if f.get("height") else "Video", "kind": "video",
                                "size": None, "url": str(f["url"]), "hls": True, "headers": {"User-Agent": UA},
                                "proxy": bool(PROXY_URL)})

        if best_audio:
            formats.append({
                "id": "srcaudio", "label": "Sound", "kind": "audio", "size": None,
                "url": best_audio["url"], "headers": _headers_of(ydl, info, best_audio), "proxy": bool(PROXY_URL),
            })

    handle = str(info.get("channel") or info.get("uploader_id") or "")
    if platform == "instagram":  # the caption is in "description"; "title" is just "Video by ..."
        caption = info.get("description") or info.get("title")
    else:
        caption = info.get("title") or info.get("description")
    caption = (caption or f"{PLATFORM_NAMES[platform]} video").strip()
    return {
        "platform": platform,
        "id": str(info.get("id") or "video"),
        "title": caption[:500],
        "author": info.get("uploader") or info.get("channel") or "",
        "handle": "" if handle.isdigit() else handle,
        "thumbnail": info.get("thumbnail"),
        "duration": to_int(info.get("duration")),
        "views": to_int(info.get("view_count")),
        "likes": to_int(info.get("like_count")),
        "comments": to_int(info.get("comment_count")),
        "shares": to_int(info.get("repost_count")) if platform == "facebook" else None,
        "created": iso_date(epoch=info.get("timestamp"), yyyymmdd=info.get("upload_date")),
        "formats": formats,
    }




# ---------------------------------------------------------------------------
# Provider: our own page reader (public videos only: reads the video links Facebook puts in the page)
# ---------------------------------------------------------------------------
PAGE_HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.facebook.com/"}
# (key in the page data, label), best first
FB_LINK_KEYS = (
    ("browser_native_hd_url", "HD"),
    ("playable_url_quality_hd", "HD"),
    ("browser_native_sd_url", "Standard"),
    ("playable_url", "Standard"),
)


def cffi_proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def _unescape_json(raw):
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace("\\/", "/")


def _meta(page, prop):
    for pattern in (rf'<meta[^>]+property="{re.escape(prop)}"[^>]+content="([^"]*)"',
                    rf'<meta[^>]+content="([^"]*)"[^>]+property="{re.escape(prop)}"'):
        m = re.search(pattern, page)
        if m:
            return htmllib.unescape(m.group(1)).strip()
    return ""


def from_own(url, platform="facebook"):
    if cffi is None:
        raise ResolveError("own: curl_cffi is not installed")
    sess = cffi.Session(impersonate=IMPERSONATE, proxies=cffi_proxies())
    try:
        r = sess.get(url, headers=PAGE_HEADERS, timeout=25, allow_redirects=True)
        if r.status_code >= 400:
            raise ResolveError(f"own: Facebook returned HTTP {r.status_code}")
        final = str(r.url)
        page = r.text or ""
    finally:
        sess.close()
    if detect_platform(final) != "facebook":
        raise ResolveError("own: the link redirected away from Facebook")

    formats, seen = [], set()
    for key, label in FB_LINK_KEYS:
        m = re.search(rf'"{key}":"(https:[^"]+)"', page)
        if not m:
            continue
        link = _unescape_json(m.group(1))
        if link in seen:
            continue
        seen.add(link)
        if any(f["label"] == label for f in formats):
            continue
        formats.append({"id": f"o{len(formats)}", "label": label, "kind": "video", "size": None,
                        "url": link, "headers": {"User-Agent": UA}, "proxy": bool(PROXY_URL)})
    if not formats:
        raise ResolveError("own: no public video link in the page (the video may need a login)")

    vid = re.search(r"(?:/videos/(?:[^/?]+/)?|[?&]v=|/reel/)(\d{6,})", final)
    dur = re.search(r'"playable_duration_in_ms":(\d+)', page)
    owner = re.search(r'"owner":\{[^{}]{0,300}?"name":"([^"]+)"', page)
    title = _meta(page, "og:title") or _meta(page, "og:description") or "Facebook video"
    return {
        "platform": "facebook",
        "id": vid.group(1) if vid else secrets.token_hex(5),
        "title": title,
        "author": _unescape_json(owner.group(1)) if owner else "",
        "handle": "",
        "thumbnail": _meta(page, "og:image") or None,
        "duration": int(dur.group(1)) // 1000 if dur else None,
        "views": None, "likes": None, "comments": None, "shares": None, "created": None,
        "formats": formats,
    }


SOCIAL_PROVIDERS = {"instagram": (from_social,), "facebook": (from_social, from_own), "pinterest": (from_social, "pinterest_page")}


def lookup_social(url, platform):
    errors, started = [], time.time()
    app.logger.info("lookup %s %s", platform, url[:140])
    for fn in SOCIAL_PROVIDERS[platform]:
        if fn == "pinterest_page":  # defined further down
            fn = from_pinterest_page
        if errors and time.time() - started > RESOLVE_BUDGET:
            errors.append("out of time")
            break
        try:
            data = fn(url, platform)
            app.logger.info("provider %s answered: %s", fn.__name__, _kinds(data["formats"]) if data else "nothing")
            if data and any(f["kind"] in ("video", "image") for f in data["formats"]):  # a Pinterest image pin has only an image
                return data
            errors.append(f"{fn.__name__}: no formats")
        except Exception as exc:  # noqa: BLE001 - try the next provider
            app.logger.warning("provider %s failed for %s: %s", fn.__name__, url, exc)
            errors.append(f"{fn.__name__}: {exc}")
    raise ResolveError(" | ".join(errors) or "no provider configured")



# ---------------------------------------------------------------------------
# Pinterest: video pins come through yt-dlp (from_social); this page reader also handles image pins
# ---------------------------------------------------------------------------
PIN_ID_RE = re.compile(r"/pin/(?:[^/?#]*--)?(\d{5,25})")


def _pin_walk(node, found, depth=0):
    """Collect the bits we need from Pinterest's embedded JSON, wherever they are nested."""
    if depth > 40 or len(found) >= 12:
        return
    if isinstance(node, dict):
        if isinstance(node.get("video_list"), dict) and "videos" not in found:
            found["videos"] = node["video_list"]
        orig = (node.get("images") or {}).get("orig") if isinstance(node.get("images"), dict) else None
        if isinstance(orig, dict) and orig.get("url") and "image" not in found:
            found["image"] = orig
            found["title"] = node.get("grid_title") or node.get("title") or ""
            found["desc"] = node.get("description") or ""
            pinner = node.get("pinner") or node.get("native_creator") or {}
            if isinstance(pinner, dict):
                found["author"] = pinner.get("full_name") or ""
                found["handle"] = pinner.get("username") or ""
            found["saves"] = (node.get("aggregated_pin_data") or {}).get("aggregated_stats", {}).get("saves") \
                if isinstance(node.get("aggregated_pin_data"), dict) else None
            found["created"] = node.get("created_at")
            found["id"] = node.get("id")
        for v in node.values():
            if isinstance(v, (dict, list)):
                _pin_walk(v, found, depth + 1)
    elif isinstance(node, list):
        for v in node[:60]:
            if isinstance(v, (dict, list)):
                _pin_walk(v, found, depth + 1)


def _pin_original(url):
    """Pinterest serves the same picture in many sizes; /originals/ is the full one."""
    return re.sub(r"/(?:\d+x|\d+x\d+|[a-z]+_?\d*)/", "/originals/", url, count=1) if "i.pinimg.com" in url else url


def _meta_name(page, name):
    for pattern in (rf'<meta[^>]+name="{re.escape(name)}"[^>]+content="([^"]*)"',
                    rf'<meta[^>]+content="([^"]*)"[^>]+name="{re.escape(name)}"'):
        m = re.search(pattern, page)
        if m:
            return htmllib.unescape(m.group(1)).strip()
    return ""


def _pin_url_ok(link):
    """True when Pinterest really serves this file (a guessed MP4 link is only used if it exists)."""
    try:
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        r = requests.get(link, headers={"User-Agent": UA, "Range": "bytes=0-0"}, timeout=8, stream=True, proxies=proxies)
        r.close()
        return r.status_code in (200, 206)
    except Exception:  # noqa: BLE001
        return False


def _pin_mp4_candidates(link):
    """Pinterest keeps plain MP4 copies next to the HLS (.m3u8) playlist:
    .../videos/mc/hls/<id>.m3u8  ->  .../videos/mc/720p/<id>.mp4  (or .../expMp4/<id>.mp4).
    Which one exists differs per pin, so several guesses are tried (best first)."""
    base = link.split("?")[0]
    if "/hls/" not in base or not base.lower().endswith(".m3u8"):
        return []
    stem = base.rsplit(".", 1)[0]
    out = []
    for q, height in (("720p", 720), ("expMp4", 0), ("1080p", 1080), ("480p", 480), ("360p", 360)):
        guess = stem.replace("/hls/", f"/{q}/", 1) + ".mp4"
        out.append((height, guess))
        if q == "expMp4" and not stem.endswith("_t1"):
            out.append((height, stem.replace("/hls/", "/expMp4/", 1) + "_t1.mp4"))
    return out


def _pin_first_mp4(link):
    """First guessed MP4 next to this HLS link that Pinterest really serves, as (height, url) - or None."""
    for height, guess in _pin_mp4_candidates(link):
        ok = _pin_url_ok(guess)
        app.logger.info("pinterest HLS->MP4 guess %s -> %s", guess[-60:], "ok" if ok else "missing")
        if ok:
            return height, guess
    return None


def _pin_main_node(node, pin_id, depth=0):
    """The dict of the pin the link points at (a page also carries 'related pins' that must not be mixed in)."""
    if depth > 40:
        return None
    if isinstance(node, dict):
        if str(node.get("id")) == pin_id and any(k in node for k in ("images", "videos", "story_pin_data")):
            return node
        for v in node.values():
            if isinstance(v, (dict, list)):
                hit = _pin_main_node(v, pin_id, depth + 1)
                if hit:
                    return hit
    elif isinstance(node, list):
        for v in node[:200]:
            if isinstance(v, (dict, list)):
                hit = _pin_main_node(v, pin_id, depth + 1)
                if hit:
                    return hit
    return None


def _pin_api(pin_id):
    """Pinterest's own pin API (what yt-dlp uses too). Used when the page itself has no video data in it."""
    api = "https://www.pinterest.com/resource/PinResource/get/"
    params = {"data": json.dumps({"options": {"field_set_key": "unauth_react_main_pin", "id": pin_id}},
                                 separators=(",", ":"))}
    headers = {"User-Agent": UA, "Accept": "application/json", "X-Pinterest-PWS-Handler": "www/[username].js",
               "X-Requested-With": "XMLHttpRequest", "Accept-Language": "en-US,en;q=0.9"}
    if cffi is not None:
        sess = cffi.Session(impersonate=IMPERSONATE, proxies=cffi_proxies())
        try:
            r = sess.get(api, params=params, headers=headers, timeout=12)
            status, body = r.status_code, r.text
        finally:
            sess.close()
    else:
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        r = requests.get(api, params=params, headers=headers, timeout=12, proxies=proxies)
        status, body = r.status_code, r.text
    if status >= 400:
        raise ResolveError(f"pinterest_api: HTTP {status}")
    data = (json.loads(body).get("resource_response") or {}).get("data")
    if not isinstance(data, dict):
        raise ResolveError("pinterest_api: no pin data")
    return data


PIN_RAW_VIDEO_RE = re.compile(r"https?://[a-z0-9.-]*pinimg\.com/videos/[^\"'\s<>\\]+?\.(?:mp4|m3u8)", re.I)


def _pin_video_links(html):
    """Last resort: any pinimg video link written anywhere in the page (also inside escaped JSON)."""
    text = html.replace("\\u002F", "/").replace("\\/", "/")
    seen, out = set(), []
    for link in PIN_RAW_VIDEO_RE.findall(text):
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


def _pin_ld_video(html):
    """schema.org VideoObject in the page's ld+json, if any: (contentUrl or '', is_video)."""
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and "VideoObject" in str(item.get("@type")):
                return str(item.get("contentUrl") or ""), True
    return "", False


def from_pinterest_page(url, platform="pinterest"):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    html = ""
    if cffi is not None:
        sess = cffi.Session(impersonate=IMPERSONATE, proxies=cffi_proxies())
        try:
            r = sess.get(url, headers=headers, timeout=25, allow_redirects=True)
            final = str(r.url)
            if r.status_code >= 400:
                raise ResolveError(f"pinterest_page: HTTP {r.status_code}")
            html = r.text
        finally:
            sess.close()
    else:
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        r = requests.get(url, headers=headers, timeout=25, allow_redirects=True, proxies=proxies)
        final = r.url
        if r.status_code >= 400:
            raise ResolveError(f"pinterest_page: HTTP {r.status_code}")
        html = r.text
    if detect_platform(final) != "pinterest":  # a short link must really lead to Pinterest
        raise ResolveError("pinterest_page: link does not lead to Pinterest")

    pin = PIN_ID_RE.search(final) or PIN_ID_RE.search(url)
    pin_id = pin.group(1) if pin else ""

    # 1) the pin's own data inside the page (its video_list and picture); related pins are ignored when the pin is known
    found, blobs, main_seen = {}, [], False
    for m in re.finditer(r'<script[^>]*id="__PWS_[A-Z_]*"[^>]*>(.*?)</script>', html, re.S):
        try:
            blobs.append(json.loads(m.group(1)))
        except ValueError:
            continue
    flagged_video = False
    for blob in blobs:
        main = _pin_main_node(blob, pin_id) if pin_id else None
        if main:
            main_seen = True
            flagged_video = flagged_video or bool(main.get("is_video") or main.get("videos")
                                                  or (main.get("story_pin_data") or {}).get("pages"))
            _pin_walk(main, found)
    if not main_seen:
        for blob in blobs:
            _pin_walk(blob, found)

    # 2) the page often has no video data (it is loaded later by script): ask Pinterest's pin API
    api_note = ""
    if not found.get("videos") and pin_id:
        try:
            api_data = _pin_api(pin_id)
            flagged_video = flagged_video or bool(api_data.get("is_video") or api_data.get("videos"))
            _pin_walk(api_data, found)
            api_note = "api ok"
        except Exception as exc:  # noqa: BLE001 - the page data may still be enough
            api_note = f"api failed: {str(exc)[:80]}"

    ld_url, ld_video = _pin_ld_video(html)
    app.logger.info("pinterest page: HTTP ok, %d bytes, final=%s, PWS scripts=%d, main_pin=%s, found=%s, og:type=%s, "
                    "og:video=%s, ld_video=%s, flagged_video=%s, %s",
                    len(html), final[:90], len(blobs), main_seen, sorted(found.keys()), _meta(html, "og:type"),
                    bool(_meta(html, "og:video")), ld_video, flagged_video, api_note)
    if found.get("videos"):
        app.logger.info("pinterest video_list: %s", [(k, (v or {}).get("height"), str((v or {}).get("url"))[-40:])
                                                     for k, v in found["videos"].items() if isinstance(v, dict)][:10])

    videos = []    # (height, width, link) - plain MP4 files
    hls_only = []  # (height, width, link) - HLS playlists (.m3u8)
    for name, v in (found.get("videos") or {}).items():
        link = (v or {}).get("url") if isinstance(v, dict) else None
        if not link:
            continue
        height, width = to_int(v.get("height")) or 0, to_int(v.get("width")) or 0
        path = link.split("?")[0].lower()
        if path.endswith(".mp4"):
            videos.append((height, width, link))
        elif path.endswith(".m3u8"):
            hls_only.append((height, width, link))
    # the page's own meta tags are another place where the video link sits
    for prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        link = _meta(html, prop) or _meta_name(html, prop)
        if link and link.split("?")[0].lower().endswith(".mp4") and not any(link == x[2] for x in videos):
            videos.append((0, 0, link))
    if ld_url and ld_url.split("?")[0].lower().endswith(".mp4") and not any(ld_url == x[2] for x in videos):
        videos.append((0, 0, ld_url))

    looks_like_video = bool(found.get("videos") or hls_only or videos or flagged_video or ld_video
                            or re.search(r'"is_video"\s*:\s*true', html)
                            or _meta(html, "og:type").lower().startswith("video") or _meta(html, "og:video")
                            or _meta_name(html, "twitter:player") or _meta(html, "twitter:player"))

    # a video pin whose data we could not read: any pinimg video link in the page is better than a cover picture
    if looks_like_video and not videos and not hls_only:
        for link in _pin_video_links(html):
            path = link.split("?")[0].lower()
            (videos if path.endswith(".mp4") else hls_only).append((0, 0, link))

    # only an HLS playlist: look for the plain MP4 beside it, else let ffmpeg save the playlist as MP4
    hls_link = None
    if not videos and hls_only:
        hls_only.sort(reverse=True)
        for height, width, link in hls_only:
            hit = _pin_first_mp4(link)
            if hit:
                videos.append((height or hit[0] or 720, width, hit[1]))
                break
        else:
            hls_link = hls_only[0]

    formats = []
    videos.sort(reverse=True)
    seen = set()
    for height, _, link in videos:
        label = f"{height}p" if height else "Video"
        if label in seen:
            continue
        seen.add(label)
        formats.append({"id": f"p{len(formats)}", "label": label, "kind": "video", "size": None, "url": link,
                        "headers": {"User-Agent": UA}, "proxy": bool(PROXY_URL)})
    if hls_link and not formats:
        formats.append({"id": "p0", "label": f"{hls_link[0]}p" if hls_link[0] else "Video", "kind": "video",
                        "size": None, "url": hls_link[2], "hls": True, "headers": {"User-Agent": UA},
                        "proxy": bool(PROXY_URL)})

    # A video pin must never silently turn into its cover picture: say so instead.
    if looks_like_video and not formats:
        raise ResolveError("pinterest_page: this is a video pin but its video link could not be read")

    image = found.get("image") or {}
    thumb = image.get("url") or _meta(html, "og:image") or None
    original = _pin_original(thumb) if thumb else None
    if original:
        formats.append({
            "id": "cover", "label": "Image (original size)" if not formats else "Cover image", "kind": "image", "size": None,
            "url": original, "fallback_url": thumb if thumb != original else None,
            "headers": {"User-Agent": UA},
        })
    if not formats:
        raise ResolveError("pinterest_page: no video or image found in the page")

    title = (found.get("title") or _meta(html, "og:title") or found.get("desc") or "Pinterest pin").strip()
    return {
        "platform": "pinterest",
        "id": str(found.get("id") or pin_id or "pin"),
        "title": title[:500],
        "author": found.get("author") or "",
        "handle": found.get("handle") or "",
        "thumbnail": thumb,
        "duration": None, "views": None, "likes": None, "comments": None,
        "shares": to_int(found.get("saves")),
        "created": None,
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# TikTok providers: our own page reader, TikWM (works from data-centre IPs), yt-dlp
# ---------------------------------------------------------------------------
_tikwm_lock = threading.Lock()
_tikwm_last = 0.0


def _tikwm_abs(u):
    if not u:
        return None
    return u if u.startswith("http") else "https://www.tikwm.com" + u


def from_tikwm(url):
    global _tikwm_last
    with _tikwm_lock:  # the free API allows about 1 request per second
        wait = 1.2 - (time.time() - _tikwm_last)
        if wait > 0:
            time.sleep(wait)
        _tikwm_last = time.time()

    r = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": url, "hd": 1},
        headers={"User-Agent": UA},
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    d = payload.get("data")
    if payload.get("code") != 0 or not d:
        raise ResolveError(f"tikwm: {payload.get('msg') or 'no data'}")

    formats = []
    if d.get("hdplay"):
        formats.append({"id": "hd", "label": "HD", "kind": "video",
                        "size": to_int(d.get("hd_size")), "url": _tikwm_abs(d["hdplay"])})
    if d.get("play"):
        formats.append({"id": "sd", "label": "Standard", "kind": "video",
                        "size": to_int(d.get("size")), "url": _tikwm_abs(d["play"])})
    if d.get("wmplay"):
        formats.append({"id": "wm", "label": "With TikTok watermark", "kind": "video",
                        "size": to_int(d.get("wm_size")), "url": _tikwm_abs(d["wmplay"])})
    if d.get("music"):
        formats.append({"id": "srcaudio", "label": "Sound", "kind": "audio",
                        "size": None, "url": _tikwm_abs(d["music"])})

    author = d.get("author") or {}
    return {
        "platform": "tiktok",
        "id": str(d.get("id") or "video"),
        "title": (d.get("title") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("unique_id") or "",
        "thumbnail": _tikwm_abs(d.get("origin_cover") or d.get("cover")),
        "duration": to_int(d.get("duration")),
        "views": to_int(d.get("play_count")),
        "likes": to_int(d.get("digg_count")),
        "comments": to_int(d.get("comment_count")),
        "shares": to_int(d.get("share_count")),
        "created": iso_date(epoch=d.get("create_time")),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Provider: yt-dlp
# ---------------------------------------------------------------------------
def from_tiktok_ytdlp(url):
    with ydl_session("tiktok", skip_download=True) as ydl:
        info = ydl.extract_info(url, download=False)
        candidates = info.get("formats") or [info]
        candidates = sorted(
            candidates,
            key=lambda f: (f.get("height") or 0, f.get("filesize") or f.get("filesize_approx") or 0),
            reverse=True,
        )

        formats, seen = [], set()
        for f in candidates:
            if not f.get("url") or f.get("vcodec") == "none":
                continue
            if (f.get("protocol") or "https") not in ("http", "https"):
                continue
            note = (f.get("format_note") or "").lower()
            if f.get("format_id") == "download" or "watermark" in note:
                label = "Original (watermark)"
            elif f.get("height"):
                label = f"{f['height']}p"
            else:
                label = str(f.get("format_id") or "Video")
            if label in seen:
                continue
            seen.add(label)

            headers = dict(f.get("http_headers") or info.get("http_headers") or {})
            cookie = ydl.cookiejar.get_cookie_header(f["url"])
            if cookie:
                headers["Cookie"] = cookie
            formats.append({
                "id": f"y{len(formats)}", "label": label, "kind": "video",
                "size": to_int(f.get("filesize") or f.get("filesize_approx")),
                "url": f["url"], "headers": headers, "proxy": bool(PROXY_URL),
            })

    return {
        "platform": "tiktok",
        "id": str(info.get("id") or "video"),
        "title": (info.get("title") or info.get("description") or "TikTok video").strip(),
        "author": info.get("uploader") or info.get("creator") or "",
        "handle": info.get("uploader_id") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": to_int(info.get("duration")),
        "views": to_int(info.get("view_count")),
        "likes": to_int(info.get("like_count")),
        "comments": to_int(info.get("comment_count")),
        "shares": to_int(info.get("repost_count")),
        "created": iso_date(epoch=info.get("timestamp"), yyyymmdd=info.get("upload_date")),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Provider: our own scraper (reads the data TikTok embeds in the video page)
# ---------------------------------------------------------------------------
TT_PAGE_HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.tiktok.com/"}
TT_DATA_RE = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


def tt_parse_item(html):
    """Pull the video object out of the JSON blob TikTok puts in the page."""
    match = TT_DATA_RE.search(html or "")
    if not match:
        raise ResolveError("own: page had no video data (TikTok probably served a block page)")
    try:
        data = json.loads(match.group(1))
    except ValueError:
        raise ResolveError("own: video data was not valid JSON")
    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}
    code = detail.get("statusCode")
    if code not in (None, 0):
        raise ResolveError(f"own: TikTok status {code} {detail.get('statusMsg') or ''}".strip())
    item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not item:
        raise ResolveError("own: video not found in page")
    return item


def tt_quality_of(bitrate_info, addr):
    """TikTok names quality by the shorter side, e.g. 720p for 720x1280."""
    w, h = addr.get("Width") or 0, addr.get("Height") or 0
    short = min(w, h) if w and h else 0
    if not short:
        m = re.search(r"_(\d{3,4})_", bitrate_info.get("GearName") or "")
        short = int(m.group(1)) if m else 0
    return short


def tt_own_data(item, cookies):
    video = item.get("video") or {}
    if item.get("imagePost") and not video.get("playAddr"):
        raise ResolveError("own: this is a photo post, not a video")

    media = {
        "headers": {"Referer": "https://www.tiktok.com/"},
        "cookies": cookies,
        "impersonate": True,
        "proxy": bool(PROXY_URL),
    }

    options = []
    for b in video.get("bitrateInfo") or []:
        addr = b.get("PlayAddr") or {}
        urls = addr.get("UrlList") or []
        if not urls:
            continue
        codec = (b.get("CodecType") or "").lower()
        options.append((tt_quality_of(b, addr), "h264" in codec, b.get("Bitrate") or 0, addr, urls[0]))
    # best quality first; within the same quality prefer h264 (plays everywhere)
    options.sort(key=lambda o: (o[0], o[1], o[2]), reverse=True)

    formats, seen = [], set()
    for short, _, _, addr, url in options:
        label = f"{short}p" if short else "Video"
        if label in seen:
            continue
        seen.add(label)
        formats.append({"id": f"o{len(formats)}", "label": label, "kind": "video",
                        "size": to_int(addr.get("DataSize")), "url": url, **media})

    play, dl = video.get("playAddr"), video.get("downloadAddr")
    if not formats and (play or dl):
        formats.append({"id": "o0", "label": video.get("definition") or "Standard", "kind": "video",
                        "size": None, "url": play or dl, **media})
    if dl and all(f["url"] != dl for f in formats):
        formats.append({"id": f"o{len(formats)}", "label": "Original (download link)", "kind": "video",
                        "size": None, "url": dl, **media})
    if not formats:
        raise ResolveError("own: no video links in page")

    audio = (item.get("music") or {}).get("playUrl")
    if audio:
        formats.append({"id": "srcaudio", "label": "Sound", "kind": "audio",
                        "size": None, "url": audio, **media})

    stats = item.get("stats") or {}
    author = item.get("author") or {}
    return {
        "platform": "tiktok",
        "id": str(item.get("id") or "video"),
        "title": (item.get("desc") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("uniqueId") or "",
        "thumbnail": video.get("originCover") or video.get("cover") or video.get("dynamicCover"),
        "duration": to_int(video.get("duration")),
        "views": to_int(stats.get("playCount")),
        "likes": to_int(stats.get("diggCount")),
        "comments": to_int(stats.get("commentCount")),
        "shares": to_int(stats.get("shareCount")),
        "created": iso_date(epoch=item.get("createTime")),
        "formats": formats,
    }


def from_tiktok_own(url):
    if cffi is None:
        raise ResolveError("own: curl_cffi is not installed")
    sess = cffi.Session(impersonate=IMPERSONATE, proxies=cffi_proxies())
    try:
        r = sess.get(url, headers=TT_PAGE_HEADERS, timeout=25, allow_redirects=True)
        if r.status_code >= 400:
            raise ResolveError(f"own: TikTok returned HTTP {r.status_code}")
        try:
            item = tt_parse_item(r.text)
        except ResolveError:
            # second try through the generic video page, which is sometimes less strict
            m = re.search(r"/(?:video|photo)/(\d+)", str(r.url))
            if not m:
                raise
            r = sess.get(f"https://www.tiktok.com/@i/video/{m.group(1)}", headers=TT_PAGE_HEADERS, timeout=25)
            item = tt_parse_item(r.text)
        cookies = sess.cookies.get_dict()
    finally:
        sess.close()
    return tt_own_data(item, cookies)


TIKTOK_FUNCS = {"own": from_tiktok_own, "tikwm": from_tikwm, "ytdlp": from_tiktok_ytdlp}


def lookup_tiktok(url):
    errors, started = [], time.time()
    for name in TIKTOK_PROVIDERS:
        fn = TIKTOK_FUNCS.get(name)
        if not fn:
            continue
        if errors and time.time() - started > RESOLVE_BUDGET:
            errors.append("out of time")
            break
        try:
            data = fn(url)
            if data and any(f["kind"] == "video" for f in data["formats"]):
                return data
            errors.append(f"{name}: no formats")
        except Exception as exc:  # noqa: BLE001 - try the next provider
            app.logger.warning("tiktok provider %s failed for %s: %s", name, url, exc)
            errors.append(f"{name}: {exc}")
    raise ResolveError(" | ".join(errors) or "no provider configured")


def finalize(info):
    """Add the cover image as an internal format so it can be downloaded like the rest."""
    if info["platform"] == "youtube":
        vid = info["id"]
        if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid or ""):
            info["formats"].append({
                "id": "cover", "label": "Cover image", "kind": "image", "size": None,
                "url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",      # not every video has this one,
                "fallback_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",  # but this one always exists
                "headers": {"User-Agent": UA},
            })
    elif info.get("thumbnail") and not any(f["id"] == "cover" for f in info["formats"]):
        info["formats"].append({
            "id": "cover", "label": "Cover image", "kind": "image", "size": None, "url": info["thumbnail"],
            "headers": {"User-Agent": UA, "Referer": f"https://www.{PLATFORMS[info['platform']][0]}/"},
        })
    return info


def resolve(url, fresh=False):
    if not fresh:
        hit = cache_get(url)
        if hit:
            return hit
    platform = detect_platform(url)
    if not platform:
        raise UserError(LINK_ERROR)
    if platform == "youtube":
        data = from_youtube(url)
    elif platform == "tiktok":
        data = lookup_tiktok(url)
    else:
        data = lookup_social(url, platform)
    data = finalize(data)
    cache_set(url, data)
    return data


def videos_of(info):
    return [f for f in info["formats"] if f["kind"] == "video"]


def find_format(info, fid):
    for f in info["formats"]:
        if f["id"] == fid:
            return f
    return None


def public_formats(info):
    """What the page sees: video qualities, one MP3, and the Document tab entries."""
    vids = videos_of(info)
    out = [{"id": f["id"], "label": f["label"], "kind": "video", "size": f.get("size"),
            "merge": bool(f.get("audio_url") or f.get("hls"))} for f in vids]
    if vids:
        out.append({"id": "mp3", "label": "MP3 audio", "kind": "audio", "size": None})
    if vids:
        out.append({"id": "file", "label": "Original video file", "kind": "document", "size": vids[0].get("size"),
                    "merge": bool(vids[0].get("audio_url") or vids[0].get("hls"))})
    if find_format(info, "cover"):
        out.append({"id": "cover", "label": find_format(info, "cover").get("label") or "Cover image", "kind": "document",
                    "size": find_format(info, "cover").get("size")})
    out.append({"id": "txt", "label": "Video details (TXT)", "kind": "document", "size": None})
    return out


# ---------------------------------------------------------------------------
# Opening a media link
# ---------------------------------------------------------------------------
def open_upstream(fmt):
    if fmt.get("impersonate"):  # TikTok's own video links only answer a Chrome-like client that has the page cookies
        if cffi is None:
            raise ResolveError("curl_cffi is not installed")
        upstream = cffi.get(
            fmt["url"],
            headers={**(fmt.get("headers") or {}), "Range": "bytes=0-"},
            cookies=fmt.get("cookies") or None,
            impersonate=IMPERSONATE,
            stream=True,
            timeout=30,
            proxies=cffi_proxies() if fmt.get("proxy") else None,
        )
        upstream.raise_for_status()
        return upstream
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if fmt.get("proxy") and PROXY_URL else None
    upstream = requests.get(
        fmt["url"],
        headers=fmt.get("headers") or {"User-Agent": UA},
        stream=True,
        timeout=30,
        proxies=proxies,
    )
    upstream.raise_for_status()
    return upstream


def open_format(url, fid):
    """Find the link for this option and open it. Retries once with fresh links (they expire), and a
    missing high-res YouTube cover falls back to a smaller one."""
    last = None
    for fresh in (False, True):
        try:
            info = resolve(url, fresh=fresh)
            fmt = find_format(info, fid)
            if not fmt:
                raise ResolveError("That download option is not available for this video.")
            try:
                return info, fmt, open_upstream(fmt)
            except Exception:  # noqa: BLE001
                if not fmt.get("fallback_url"):
                    raise
                return info, fmt, open_upstream({**fmt, "url": fmt["fallback_url"]})
        except Exception as exc:  # noqa: BLE001
            last = exc
            app.logger.warning("open_format try failed for %s: %s", url, exc)
    raise last


def fmt_duration(seconds):
    if not seconds:
        return "-"
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_count(n):
    return f"{n:,}" if isinstance(n, int) else "-"


def build_details(info, url):
    """A plain-text sheet with the video's details (UTF-8, works with any language)."""
    name = PLATFORM_NAMES[info["platform"]]
    who = info["author"] or "-"
    if info["handle"]:
        who += f" (@{info['handle']})"
    tags = info.get("tags") or re.findall(r"#[^\s#]+", info["title"])
    heading = f"{name} video details"
    lines = [
        heading,
        "=" * len(heading),
        "",
        f"Title   : {info['title']}",
        f"Creator : {who}",
        f"Posted  : {info['created'] or '-'}",
        f"Length  : {fmt_duration(info['duration'])}",
        "",
        f"Views    : {fmt_count(info['views'])}",
        f"Likes    : {fmt_count(info['likes'])}",
        f"Comments : {fmt_count(info['comments'])}",
    ]
    if info.get("shares") is not None:
        lines.append(f"Shares   : {fmt_count(info['shares'])}")
    lines += [
        "",
        f"Tags : {', '.join(tags) if tags else '-'}",
        f"Link : {url}",
        "",
        "Saved with Rio Downloader",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live download counter
# ---------------------------------------------------------------------------
_count = 0
_count_lock = threading.Lock()
_persist_lock = threading.Lock()


def _upstash(path):
    r = requests.get(f"{UPSTASH_URL}/{path}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
    r.raise_for_status()
    return r.json().get("result")


def load_count():
    global _count
    n = 0
    try:
        if UPSTASH_URL and UPSTASH_TOKEN:
            n = to_int(_upstash(f"get/{COUNTER_KEY}")) or 0
        elif os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE) as fh:
                n = to_int(json.load(fh).get("downloads")) or 0
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("could not load the download counter: %s", exc)
    with _count_lock:
        _count = max(_count, n)


def _persist_count():
    global _count
    try:
        with _persist_lock:
            if UPSTASH_URL and UPSTASH_TOKEN:
                n = to_int(_upstash(f"incr/{COUNTER_KEY}"))
                if n:
                    with _count_lock:
                        _count = max(_count, n)
            else:
                with _count_lock:
                    value = _count  # always write the newest number, never an older one
                tmp = COUNTER_FILE + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump({"downloads": value}, fh)
                os.replace(tmp, COUNTER_FILE)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("could not save the download counter: %s", exc)


def bump_count():
    global _count
    with _count_lock:
        _count += 1
    threading.Thread(target=_persist_count, daemon=True).start()


def wants_whole_file():
    """Browsers and phones sometimes probe a file with small ranges; only a full
    request from the start counts as a download."""
    rng = request.headers.get("Range", "").replace(" ", "")
    return request.method == "GET" and rng in ("", "bytes=0-")


load_count()


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
_ffmpeg = None
_job_sem = threading.Semaphore(max(1, PARALLEL_JOBS))  # downloads running at once
_enc_sem = threading.Semaphore(1)                      # one watermark encode at a time keeps the free plan alive
_jobs, _jobs_lock = {}, threading.Lock()
_files, _files_lock = {}, threading.Lock()
_tickets, _tickets_lock = {}, threading.Lock()


def ffmpeg_path():
    global _ffmpeg
    if _ffmpeg is None:
        found = shutil.which("ffmpeg")
        if not found and imageio_ffmpeg:
            try:
                found = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:  # noqa: BLE001
                found = None
        _ffmpeg = found or ""
    return _ffmpeg or None


def watermark_ready():
    return WATERMARK_ENABLED and ffmpeg_path() is not None and os.path.exists(WATERMARK_IMAGE)


def audio_ready():
    return ffmpeg_path() is not None


def probe_size(path):
    p = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path], capture_output=True, text=True, timeout=30)
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", p.stderr)
    if not m:
        raise RuntimeError("could not read the video size")
    return int(m.group(1)), int(m.group(2))


PROGRESS_KEYS = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time", "dup_frames=",
                 "drop_frames=", "speed=", "progress=")


def run_ffmpeg(cmd, duration, on_progress):
    """Run ffmpeg, report progress, raise RuntimeError with the tail of its error output."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timer = threading.Timer(FFMPEG_TIMEOUT, proc.kill)
    timer.start()
    tail = deque(maxlen=12)
    try:
        for line in proc.stdout:
            if line.startswith("out_time_ms="):  # (despite the name this is microseconds)
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if duration:
                    on_progress(max(0, min(99, int(done / duration * 100))))
            elif not line.startswith(PROGRESS_KEYS):
                tail.append(line.strip())
        proc.wait()
    finally:
        timer.cancel()
    if proc.returncode != 0:
        if proc.returncode < 0:
            raise RuntimeError("ffmpeg took too long and was stopped")
        raise RuntimeError("ffmpeg failed: " + " ".join(tail)[-300:])


def watermark_filter(width, pos):
    logo_w = max(120, int(width * WATERMARK_SIZE))
    logo_w -= logo_w % 2
    margin = max(10, int(width * 0.03))
    x = f"W-w-{margin}" if pos.endswith("r") else str(margin)
    y = f"H-h-{margin}" if pos.startswith("b") else str(margin)
    # the last two filters make the size even, which libx264 + yuv420p requires
    return (
        f"[1:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa={WATERMARK_OPACITY}[wm];"
        f"[0:v][wm]overlay={x}:{y}:format=auto,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
    )


# ---------------------------------------------------------------------------
# Temporary stores: tickets (direct downloads), files (ffmpeg results), jobs
# ---------------------------------------------------------------------------
def purge_files():
    now = time.time()
    with _files_lock:
        for token in [t for t, f in _files.items() if f["exp"] < now]:
            shutil.rmtree(_files.pop(token)["dir"], ignore_errors=True)
        while len(_files) > 20:
            oldest = min(_files, key=lambda t: _files[t]["exp"])
            shutil.rmtree(_files.pop(oldest)["dir"], ignore_errors=True)


def purge_jobs():
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            _jobs.pop(jid, None)


def purge_tickets():
    now = time.time()
    with _tickets_lock:
        for token in [t for t, v in _tickets.items() if v["exp"] < now]:
            _tickets.pop(token, None)
        while len(_tickets) > 500:
            _tickets.pop(min(_tickets, key=lambda t: _tickets[t]["exp"]), None)


def register_file(path, name, mime, workdir):
    purge_files()
    token = secrets.token_urlsafe(16)
    with _files_lock:
        _files[token] = {"path": path, "name": name, "mime": mime, "dir": workdir,
                         "exp": time.time() + FILE_TTL, "counted": False}
    return token


def make_ticket(**fields):
    purge_tickets()
    token = secrets.token_urlsafe(16)
    with _tickets_lock:
        _tickets[token] = {"exp": time.time() + TICKET_TTL, "counted": False, "body": None, "mime": None,
                           "count": True, **fields}
    return token


# ---------------------------------------------------------------------------
# Jobs: video (with or without the watermark) and MP3
# ---------------------------------------------------------------------------
def format_selector(height):
    """(yt-dlp format string, container). Up to 1080p it asks for H.264 + AAC so the MP4 plays everywhere."""
    if height <= 1080:
        sel = (f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
               f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
               f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best")
        return sel, "mp4"
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best", "mkv"


def ytdlp_download(job, workdir, url, selector, container, stem, streams, extra_opts=None):
    """Let yt-dlp download (and join) the chosen streams into workdir. Returns the file's path."""
    seen = []

    def on_progress(d):
        if d.get("status") != "downloading":
            return
        name = d.get("filename") or ""
        if name not in seen:
            seen.append(name)
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        frac = min(1.0, (d.get("downloaded_bytes") or 0) / total) if total else 0.0
        share = (min(seen.index(name), streams - 1) + frac) / streams  # picture first, then sound
        job["percent"] = max(job.get("percent", 0), min(95, int(share * 95)))

    def on_postprocess(d):
        if d.get("status") == "started" and "merg" in str(d.get("postprocessor") or "").lower():
            job["stage"] = "merging"

    extra = {
        "format": selector,
        "outtmpl": os.path.join(workdir, stem + ".%(ext)s"),
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "max_filesize": MAX_SOURCE_MB * 1024 * 1024,
        "concurrent_fragment_downloads": 4,
    }
    if container:
        extra["merge_output_format"] = container
    with ydl_session("youtube", **{**extra, **(extra_opts or {})}) as ydl:
        ydl.extract_info(url, download=True)

    done = [n for n in os.listdir(workdir)
            if n.startswith(stem + ".") and not n.endswith((".part", ".ytdl", ".temp"))]
    if not done:  # yt-dlp skips a file above max_filesize without raising an error
        raise UserError("This video is too large to process.")
    return os.path.join(workdir, max(done, key=lambda n: os.path.getsize(os.path.join(workdir, n))))


def save_stream(upstream, dest):
    """Write an open download to disk, with a size limit."""
    size = 0
    try:
        with open(dest, "wb") as fh:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_SOURCE_MB * 1024 * 1024:
                    raise ResolveError("This video is too large to process.")
                fh.write(chunk)
    finally:
        upstream.close()
    return size


def hls_to_mp4(job, link, dest, duration, use_proxy=False):
    """Save an HLS playlist (.m3u8) as one MP4. The streams are copied, nothing is re-encoded."""
    if not ffmpeg_path():
        raise ResolveError("Video preparation isn't available on this server (ffmpeg is missing).")
    last_error = None
    for fix_audio in (True, False):  # HLS audio is usually ADTS AAC, which MP4 needs rewrapped
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
               "-user_agent", UA]
        if use_proxy and PROXY_URL.lower().startswith("http"):
            cmd += ["-http_proxy", PROXY_URL]
        cmd += ["-i", link, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
        if fix_audio:
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += ["-f", "mp4", "-movflags", "+faststart", dest]  # -f: the audio job passes a name without extension
        try:
            run_ffmpeg(cmd, duration, lambda pct: job.update(percent=pct))
            last_error = None
            break
        except RuntimeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    if os.path.getsize(dest) > MAX_SOURCE_MB * 1024 * 1024:
        raise ResolveError("This video is too large to process.")


def download_source(job, url, fid, dest):
    """Save one format to disk. Returns (info, fmt)."""
    info = resolve(url)
    fmt = find_format(info, fid)
    if fmt and fmt.get("hls"):
        hls_to_mp4(job, fmt["url"], dest, info.get("duration"), fmt.get("proxy"))
        return info, fmt
    info, fmt, upstream = open_format(url, fid)
    save_stream(upstream, dest)
    return info, fmt


def fetch_video(job, workdir, url, fid):
    """Save the chosen video as one MP4. A 'merge' option (picture stream + sound stream) is
    downloaded in two parts and joined with ffmpeg, copying the streams so nothing is re-encoded."""
    dest = os.path.join(workdir, "source.mp4")
    info = resolve(url)
    first = find_format(info, fid)
    if first and first.get("hls"):  # an HLS playlist: ffmpeg reads the pieces and writes one MP4
        hls_to_mp4(job, first["url"], dest, info.get("duration"), first.get("proxy"))
        return info, first, dest
    info, fmt, upstream = open_format(url, fid)
    if not fmt.get("audio_url"):
        save_stream(upstream, dest)
        return info, fmt, dest

    picture, sound = os.path.join(workdir, "part_video"), os.path.join(workdir, "part_audio")
    save_stream(upstream, picture)
    audio_fmt = {"url": fmt["audio_url"], "headers": fmt.get("audio_headers") or fmt.get("headers"),
                 "proxy": fmt.get("proxy")}
    save_stream(open_upstream(audio_fmt), sound)

    job["stage"] = "merging"
    last_error = None
    for audio_args in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "128k"]):
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
            "-i", picture, "-i", sound, "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", *audio_args, "-movflags", "+faststart", dest,
        ]
        try:
            run_ffmpeg(cmd, info.get("duration"), lambda pct: job.update(percent=pct))
            last_error = None
            break
        except RuntimeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    for part in (picture, sound):
        try:
            os.remove(part)
        except OSError:
            pass
    return info, fmt, dest


def job_audio_youtube(job, workdir, url):
    """Download the best sound and convert it to a real MP3 (with title and channel in its tags)."""
    info = resolve(url)
    title = re.sub(r"[\r\n\x00]", " ", info["title"])[:120]
    artist = info["author"] or info["handle"] or "YouTube"
    meta = ["-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", "album=YouTube"]

    job["stage"] = "downloading"
    src = ytdlp_download(job, workdir, url, "bestaudio[ext=m4a]/bestaudio/best", None, "audio_src", 1, info.get("ytdl_extra"))

    job["stage"] = "converting"
    job["percent"] = 0
    base = f"youtube_{safe_name(info['id'])}_audio"
    last_error = None
    for ext, codec in (("mp3", ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3"]),
                       ("m4a", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])):
        out = os.path.join(workdir, f"audio.{ext}")
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
               "-i", src, "-vn", "-map", "0:a:0", "-map_metadata", "-1", *codec, *meta, out]
        try:
            run_ffmpeg(cmd, info.get("duration"), lambda pct: job.update(percent=pct))
            if os.path.getsize(out) > 1024:
                return out, f"{base}.{ext}", "audio/mpeg" if ext == "mp3" else "audio/mp4"
        except RuntimeError as exc:
            last_error = exc
    app.logger.error("audio failed for %s: %s", url, last_error)
    raise ResolveError("Couldn't get the sound from this video.")


def job_audio_social(job, workdir, url):
    """Make a real MP3. Facebook's sound is AAC/M4A (or inside the video), so it is always converted."""
    info = resolve(url)
    sources = [f["id"] for f in info["formats"] if f["kind"] == "audio"]
    plain = [f for f in videos_of(info) if not f.get("audio_url")]  # only videos that really carry sound (an HLS one is read by ffmpeg)
    if plain:
        sources.append(plain[-1]["id"])  # the smallest one is enough to read the sound from
    title = re.sub(r"[\r\n\x00]", " ", info["title"])[:120]
    plat_name = PLATFORM_NAMES[info["platform"]]
    artist = info["author"] or info["handle"] or plat_name
    meta = ["-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", f"album={plat_name}"]
    base = f"{info['platform']}_{safe_name(info['id'])}_audio"
    last_error = None

    for idx, fid in enumerate(sources):
        src = os.path.join(workdir, f"source{idx}")
        try:
            job["stage"] = "downloading"
            download_source(job, url, fid, src)
            job["stage"] = "converting"
            for ext, codec in (("mp3", ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3"]),
                               ("m4a", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])):
                out = os.path.join(workdir, f"audio{idx}.{ext}")
                cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
                       "-i", src, "-vn", "-map", "0:a:0", "-map_metadata", "-1", *codec, *meta, out]
                try:
                    run_ffmpeg(cmd, info.get("duration"), lambda pct: job.update(percent=pct))
                    if os.path.getsize(out) > 1024:
                        return out, f"{base}.{ext}", "audio/mpeg" if ext == "mp3" else "audio/mp4"
                except RuntimeError as exc:
                    last_error = exc
        except ResolveError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("audio source %s failed for %s: %s", fid, url, exc)
            last_error = exc
    app.logger.error("audio failed for %s: %s", url, last_error)
    if isinstance(last_error, ResolveError):
        raise last_error
    raise ResolveError("Couldn't get the sound from this video. It may have no audio.")


def burn_watermark(job, src, workdir, pos, duration):
    """Burn the Rio ghost into the video (one encode at a time keeps the free plan alive)."""
    job["stage"] = "watermarking"
    job["percent"] = 0
    width, _ = probe_size(src)
    out = os.path.join(workdir, "output.mp4")
    last_error = None
    with _enc_sem:
        # copy is instant; re-encode the sound only if the MP4 container refuses it
        for audio_args in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "128k"]):
            cmd = [
                ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
                "-i", src, "-i", WATERMARK_IMAGE,
                "-filter_complex", watermark_filter(width, pos),
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", "28",
                *audio_args, "-movflags", "+faststart", out,
            ]
            try:
                run_ffmpeg(cmd, duration, lambda pct: job.update(percent=pct))
                last_error = None
                break
            except RuntimeError as exc:
                last_error = exc
    if last_error:
        raise last_error
    return out


def job_video(job, workdir, url, fid, pos, wm, doc):
    """A video: get the file (YouTube: yt-dlp joins picture + sound; others: download, join if needed),
    then optionally burn in the Rio watermark."""
    info = resolve(url)
    plat = info["platform"]
    sid = safe_name(info["id"])
    job["stage"] = "downloading"
    if plat == "youtube":
        vids = videos_of(info)
        fmt = vids[0] if (doc and vids) else find_format(info, fid)
        if not fmt or fmt["kind"] != "video":
            raise ResolveError("That download option is not available for this video.")
        selector, container = format_selector(fmt["height"])
        src = ytdlp_download(job, workdir, url, selector, container, "video", 2, info.get("ytdl_extra"))
        ext = os.path.splitext(src)[1].lower() or ".mp4"
    else:
        info, fmt, src = fetch_video(job, workdir, url, fid)
        if fmt["kind"] != "video":
            raise ResolveError("Only videos can be saved this way.")
        ext = ".mp4"

    if not wm:
        if doc:
            return src, f"{plat}_{sid}_original{ext}", "application/octet-stream"
        return src, f"{plat}_{sid}_{safe_name(fmt['label'])}{ext}", "video/mp4" if ext == ".mp4" else "video/x-matroska"

    out = burn_watermark(job, src, workdir, pos, info.get("duration"))
    return out, f"{plat}_{sid}_rio.mp4", "video/mp4"


def job_audio(job, workdir, url):
    if detect_platform(url) == "youtube":
        return job_audio_youtube(job, workdir, url)
    return job_audio_social(job, workdir, url)


def run_job(job_id, mode, url, fid, pos, wm, doc):
    job = _jobs[job_id]
    workdir = tempfile.mkdtemp(prefix="job_")
    got_slot = False
    try:
        if not _job_sem.acquire(timeout=240):
            raise ResolveError("The server is busy. Try again in a minute.")
        got_slot = True
        if mode == "audio":
            out, name, mime = job_audio(job, workdir, url)
        else:
            out, name, mime = job_video(job, workdir, url, fid, pos, wm, doc)
        token = register_file(out, name, mime, workdir)
        workdir = None  # the file registry owns the folder from now on
        job.update(state="done", stage="done", percent=100, file=f"/file/{token}")
    except ResolveError as exc:
        job.update(state="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("%s job failed for %s", mode, url)
        if mode == "audio":
            default = "Couldn't prepare the sound. Try again."
        elif wm:
            default = "Couldn't prepare the watermark. Try again, or switch the watermark off."
        else:
            default = "Couldn't prepare this video. Try again, or pick another quality."
        job.update(state="error", error=explain(exc, default, detect_platform(url)) + (f" ({str(exc)[:200]})" if DEBUG_ERRORS else ""))
    finally:
        if got_slot:
            _job_sem.release()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def start_job(mode, url, fid, pos, wm, doc):
    """Register a job and start it in a thread. Returns the JSON reply for /api/prepare."""
    purge_jobs()
    with _jobs_lock:
        if sum(1 for j in _jobs.values() if j["state"] == "working") >= MAX_ACTIVE_JOBS:
            return error("Lots of videos are being processed right now. Try again in a minute.", 503)
        job_id = secrets.token_urlsafe(8)
        _jobs[job_id] = {"state": "working", "stage": "queued", "percent": 0, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, mode, url, fid, pos, wm, doc), daemon=True).start()
    return jsonify({"job": job_id})


# ---------------------------------------------------------------------------
# Static assets get a version so a new deploy is never served from an old cache
# ---------------------------------------------------------------------------
def asset_version():
    newest = 0
    static = os.path.join(BASE_DIR, "static")
    try:
        for name in os.listdir(static):
            newest = max(newest, int(os.path.getmtime(os.path.join(static, name))))
    except OSError:
        pass
    return newest


ASSET_V = asset_version()


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if resp.mimetype == "text/html":
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
    return resp


API_PREFIXES = ("/api/", "/dl/", "/file/")


@app.errorhandler(Exception)
def handle_any_error(exc):
    """The API always answers in JSON, so the page can show a clear message."""
    api = request.path.startswith(API_PREFIXES)
    if isinstance(exc, HTTPException):
        if not api:
            return exc
        messages = {404: "Not found.", 405: "That request type is not allowed.", 413: "The request was too large.",
                    400: "The request was not understood."}
        return error(messages.get(exc.code, "The request failed."), exc.code or 500)
    app.logger.exception("unhandled error on %s", request.path)
    if api:
        return error("Something went wrong on our side. Try again.", 500, exc)
    return Response("Something went wrong. Please try again.", status=500, mimetype="text/plain")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", v=ASSET_V)


@app.get("/manifest.webmanifest")
def web_manifest():
    """Makes the site installable on phones and desktops (PWA)."""
    icons = [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
        {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ]
    data = {
        "id": "/",
        "name": "RIO x SOCIAL MEDIA DOWNLOADER",
        "short_name": "RIO Downloader",
        "description": "Paste a YouTube, Instagram, Facebook, TikTok or Pinterest link and save the video, MP3, original file or cover.",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "background_color": "#050505",
        "theme_color": "#050505",
        "categories": ["utilities", "social", "multimedia"],
        "icons": icons,
        # Android: lets the user "Share > RIO Downloader" straight from the YouTube / TikTok / Instagram app
        "share_target": {"action": "/", "method": "GET", "params": {"title": "title", "text": "text", "url": "url"}},
    }
    resp = Response(json.dumps(data), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/sw.js")
def service_worker():
    """Served from the site root so it can control every page. The version is baked in so a new deploy updates it."""
    resp = Response(render_template("sw.js", v=ASSET_V), mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/debug/ip")
def debug_ip():
    """Only with DEBUG_ERRORS=1: shows which address the rate limit sees (to tune PROXY_HOPS)."""
    if not DEBUG_ERRORS:
        return error("Not found.", 404)
    return jsonify({"seen_as": client_ip(), "remote_addr": request.remote_addr,
                    "x_forwarded_for": request.headers.get("X-Forwarded-For", ""), "proxy_hops": PROXY_HOPS})


@app.get("/api/debug/env")
def debug_env():
    """Only with DEBUG_ERRORS=1: what this server has available (yt-dlp, JS runtime, ffmpeg, cookies per site)."""
    if not DEBUG_ERRORS:
        return error("Not found.", 404)
    try:
        version = yt_dlp.version.__version__ if yt_dlp else None
    except AttributeError:
        version = "unknown"
    return jsonify({"yt_dlp": version, "ffmpeg": ffmpeg_path(), "deno": shutil.which("deno"),
                    "node": shutil.which("node"), "bun": shutil.which("bun"),
                    "cookies": {k: bool(v) for k, v in COOKIES_TEXT.items()}, "curl_cffi": cffi is not None,
                    "proxy": bool(PROXY_URL)})


@app.post("/api/info")
def api_info():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    url = extract_url(text_field(json_body(), "url"))
    platform = detect_platform(url)
    if not platform:
        return error(LINK_ERROR, 400)

    try:
        info = resolve(url)
    except Exception as exc:  # noqa: BLE001
        app.logger.error("resolve failed for %s: %s", url, exc)
        name = PLATFORM_NAMES[platform]
        return error(explain(exc, f"Couldn't get this video. It may be private or removed, or {name} is "
                                  "limiting this server. Try again in a moment.", platform), 422, exc)

    app.logger.info("info ok %s id=%s formats=[%s]", platform, info.get("id"), _kinds(info.get("formats")))
    return jsonify({
        **{k: v for k, v in info.items() if k not in ("formats", "tags", "ytdl_extra")},
        "platform_name": PLATFORM_NAMES[platform],
        "formats": public_formats(info),
        "watermark": watermark_ready(),
        "positions": list(WATERMARK_POSITIONS),
        "position": WATERMARK_DEFAULT_POS,
    })


@app.post("/api/prepare")
def api_prepare():
    """Start any download. Replies {"file": ...} (ready now) or {"job": ...} (poll it)."""
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    data = json_body()
    url = extract_url(text_field(data, "url"))
    fid = text_field(data, "f", 24)
    pos = text_field(data, "pos", 2)
    want_wm = data.get("wm") is True
    platform = detect_platform(url)
    if not platform:
        return error(LINK_ERROR, 400)
    if not FORMAT_ID_RE.match(fid):
        return error("Unknown download option.", 400)
    if pos not in WATERMARK_POSITIONS:
        pos = WATERMARK_DEFAULT_POS

    # ---- MP3 -------------------------------------------------------------------------------
    if fid == "mp3":
        if not audio_ready():
            return error("MP3 conversion isn't available on this server.", 501)
        return start_job("audio", url, fid, pos, False, False)

    # ---- everything else needs the video's details first ---------------------------------------
    try:
        info = resolve(url)
    except Exception as exc:  # noqa: BLE001
        app.logger.error("resolve failed for %s: %s", url, exc)
        return error(explain(exc, "Couldn't get this video. It may be private or removed. Try again in a moment.",
                             platform), 422, exc)

    sid = safe_name(info["id"])
    if fid == "txt":
        body = build_details(info, url).encode("utf-8-sig")  # BOM keeps Sinhala readable in old Notepad
        token = make_ticket(body=body, mime="text/plain; charset=utf-8", name=f"{platform}_{sid}_details.txt",
                            count=False)
        return jsonify({"file": f"/dl/{token}"})

    if fid == "cover":
        if not find_format(info, "cover"):
            return error("This video has no cover image.", 404)
        try:
            _, _, probe = open_format(url, "cover")
            probe.close()
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("cover probe failed for %s: %s", url, exc)
            return error("Couldn't download the cover image. Try again in a moment.", 502, exc)
        token = make_ticket(url=url, fid="cover", name=f"{platform}_{sid}_cover", mime=None, count=False)
        return jsonify({"file": f"/dl/{token}"})

    # ---- a video: pick the option ----------------------------------------------------------------
    if fid == "file":
        vids = videos_of(info)
        if not vids:
            return error("No video file is available for this link.", 404)
        fmt, as_doc = vids[0], True
        target, name = fmt["id"], f"{platform}_{sid}_original.mp4"
    else:
        fmt = find_format(info, fid)
        if not fmt or fmt["kind"] != "video":
            return error("Unknown download option.", 400)
        as_doc = False
        target, name = fid, f"{platform}_{sid}_{safe_name(fmt['label'])}.mp4"

    wm_on = want_wm and not as_doc
    merge = bool(fmt.get("audio_url")) or bool(fmt.get("hls"))  # HLS playlists are saved as MP4 by ffmpeg in a job
    if wm_on and not watermark_ready():
        return error("Watermarking isn't available on this server.", 501)

    # ---- a job: YouTube always, Instagram / Facebook / TikTok / Pinterest when joining picture + sound or watermarking -----
    if platform == "youtube" or wm_on or merge:
        if not ffmpeg_path():
            return error("Video preparation isn't available on this server (ffmpeg is missing).", 501)
        return start_job("video", url, target, pos, wm_on, as_doc)

    # ---- direct download: check that the link really works, then hand out a ticket ------------------
    try:
        _, _, probe = open_format(url, target)  # fails here, with a message, if the site refuses the link
        probe.close()
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("probe failed for %s (%s): %s", url, target, exc)
        return error("Couldn't download this one. Try again in a moment.", 502, exc)

    token = make_ticket(url=url, fid=target, name=name, mime="application/octet-stream" if as_doc else None,
                        count=True)
    return jsonify({"file": f"/dl/{token}"})


@app.get("/dl/<token>")
def serve_ticket(token):
    purge_tickets()
    with _tickets_lock:
        t = _tickets.get(token)
    if not t:
        return error("This download link has expired. Start the download again.", 404)

    def count_once():
        if t["count"] and not t["counted"] and wants_whole_file():
            t["counted"] = True
            bump_count()

    if t["body"] is not None:
        count_once()
        resp = Response(t["body"], content_type=t["mime"])
        resp.headers["Content-Disposition"] = f'attachment; filename="{t["name"]}"'
        resp.headers["Cache-Control"] = "no-store"
        return resp

    try:
        _, fmt, upstream = open_format(t["url"], t["fid"])
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("download failed for %s", t["url"])
        return error("Couldn't download this one. Start it again in a moment.", 502, exc)
    count_once()

    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    up_type = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    name = t["name"]
    if fmt["kind"] == "image":
        ctype = up_type if up_type.startswith("image/") else "image/jpeg"
        name += {"image/webp": ".webp", "image/png": ".png"}.get(ctype, ".jpg")
    else:
        ctype = t["mime"] or (up_type if up_type.startswith(("video/", "audio/")) else "video/mp4")

    resp = Response(stream(), content_type=ctype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    resp.headers["Cache-Control"] = "no-store"
    if upstream.headers.get("Content-Length"):
        resp.headers["Content-Length"] = upstream.headers["Content-Length"]
    return resp


@app.get("/api/stats")
def api_stats():
    resp = jsonify({"downloads": _count})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return error("This job has expired. Start the download again.", 404)
    resp = jsonify({k: job.get(k) for k in ("state", "stage", "percent", "file", "error")})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/file/<token>")
def serve_file(token):
    purge_files()
    with _files_lock:
        item = _files.get(token)
    if not item or not os.path.exists(item["path"]):
        return error("This download link has expired. Start the download again.", 404)
    if not item["counted"] and wants_whole_file():
        item["counted"] = True
        bump_count()
    return send_file(item["path"], as_attachment=True, download_name=item["name"], mimetype=item["mime"], max_age=0)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_int_env("PORT", 5000), debug=os.environ.get("FLASK_DEBUG") == "1")
