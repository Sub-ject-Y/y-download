#!/usr/bin/env python3
"""
Y Download - a web app that downloads videos with yt-dlp.

Local development:
    ./run.sh                       -> http://localhost:8000

Production (Render, Docker, any WSGI host):
    gunicorn app:app --bind 0.0.0.0:$PORT

Requires python3 plus two *system* binaries:
    ffmpeg     - merges video+audio and converts to MP3
    node/deno  - solves YouTube's signature (nsig) challenges
"""

from __future__ import annotations

import base64
import hmac
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

import yt_dlp

from netguard import (
    install_urllib_redirect_guard,
    install_ydl_redirect_guard,
    validate_url,
)

try:  # optional: enables TLS/JA3 browser impersonation (see ytdlp_base_opts)
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_NAME = "Y Download"
VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# Hosting providers (Render, Railway, Fly, Heroku...) inject PORT.
# Locally we keep the loopback default so nothing is exposed on the LAN.
IS_SERVER = bool(os.environ.get("PORT")) or _env_flag("YTDL_SERVER", False)
PORT = _env_int("PORT", _env_int("YTDL_PORT", 8000))
HOST = os.environ.get("YTDL_HOST") or ("0.0.0.0" if IS_SERVER else "127.0.0.1")

# Media scratch space. On hosts with an ephemeral filesystem point this at /tmp.
DOWNLOAD_DIR = Path(os.environ.get("YTDL_MEDIA_DIR") or (BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# how long a finished file stays on disk before being deleted (seconds)
JOB_TTL_SECONDS = _env_int("YTDL_TTL_SECONDS", 600)
# how often the janitor wakes up (seconds)
JANITOR_INTERVAL = _env_int("YTDL_JANITOR_INTERVAL", 60)
# Wipe unknown leftover folders on start / during the sweep. Off locally so it
# can never eat files you downloaded by hand.
SWEEP_ORPHANS = _env_flag("YTDL_SWEEP_ORPHANS", True)

# Abuse limits (a public instance is an open download proxy - protect it)
MAX_CONCURRENT_JOBS = _env_int("YTDL_MAX_CONCURRENT_JOBS", 3)
RATE_LIMIT_JOBS = _env_int("YTDL_RATE_LIMIT_JOBS", 10)
RATE_LIMIT_PREVIEWS = _env_int("YTDL_RATE_LIMIT_PREVIEWS", 60)
# Optional hard ceiling on all requests per hour, shared by every client. A
# safety net for when the per-IP key cannot be trusted. 0 disables it.
RATE_LIMIT_GLOBAL = _env_int("YTDL_RATE_LIMIT_GLOBAL", 0)
RATE_LIMIT_WINDOW = 3600

# Request body cap (the JSON bodies here are tiny), plus a per-download size cap
# and a free-space floor so a job can never fill the disk. All in the units the
# variable name says; 0 disables that particular guard.
MAX_REQUEST_BYTES = _env_int("YTDL_MAX_REQUEST_BYTES", 16 * 1024)
MAX_FILESIZE_MB = _env_int("YTDL_MAX_FILESIZE_MB", 2048)
MIN_FREE_MB = _env_int("YTDL_MIN_FREE_MB", 512)

# How many reverse proxies to trust when reading X-Forwarded-For. 1 is right for
# Render (its edge appends the real client IP). 0 ignores the header entirely.
TRUSTED_PROXY_HOPS = _env_int("YTDL_TRUSTED_PROXY_HOPS", 1 if IS_SERVER else 0)

# Optional HTTP Basic auth. Empty (the default) means the app is open.
ACCESS_TOKEN = os.environ.get("YTDL_ACCESS_TOKEN") or ""
ACCESS_USER = os.environ.get("YTDL_USERNAME") or "y-download"
# When on, refuse to start in server mode without a token instead of warning.
REQUIRE_AUTH = _env_flag("YTDL_REQUIRE_AUTH", False)
# When on, refuse to start if the SSRF redirect guard cannot be installed on
# yt-dlp's handlers instead of only warning.
REQUIRE_GUARD = _env_flag("YTDL_REQUIRE_GUARD", False)

# Optional cookies / proxy, needed when YouTube blocks the host's datacenter IP.
PROXY = os.environ.get("YTDL_PROXY") or ""
COOKIES_B64 = os.environ.get("YTDL_COOKIES_B64") or ""
COOKIES_FILE = Path(os.environ.get("YTDL_COOKIES_FILE") or (BASE_DIR / ".cookies.txt"))

# Impersonate a real browser for the generic extractor. Many sites answer
# datacenter IPs with a Cloudflare 403; this is the documented remedy.
GENERIC_IMPERSONATE = _env_flag("YTDL_GENERIC_IMPERSONATE", True)

# When on, /info may hand the browser a direct media URL so eligible single-file
# sources are fetched straight from the CDN, skipping server-side storage and
# the /file hop entirely. Off by default, so production keeps the existing
# server-side flow; ineligible sources always fall back to it regardless.
DIRECT_DOWNLOAD = _env_flag("YTDL_DIRECT", False)

# The quality choices offered in the UI -> yt-dlp format selector strings.
#
# The trailing "/b" is an important fallback: sources that expose a single
# pre-muxed file with no height metadata (direct .mp4 links, some sites' only
# format) match nothing in the capped selectors, so without it a "720p" request
# fails with "Requested format is not available".
QUALITY_FORMATS: dict[str, str] = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "ba/b",
}

QUALITY_LABELS = {
    "best": "Best quality (MP4)",
    "1080": "1080p (MP4)",
    "720": "720p (MP4)",
    "480": "480p (MP4)",
    "audio": "Audio only (MP3 192k)",
}

ALLOWED_COOKIE_BROWSERS = {"chrome", "chromium", "firefox", "edge", "brave", "opera", "vivaldi", "safari"}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Job folders are always `uuid4().hex[:12]`. Restricting sweeps to this shape
# means a stray file in the media directory is never deleted by accident.
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
if TRUSTED_PROXY_HOPS > 0:
    # Trust exactly N proxy hops when rewriting the client IP. Anything the
    # client put in front of those hops is ignored, so X-Forwarded-For cannot be
    # used to forge an identity.
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=TRUSTED_PROXY_HOPS, x_proto=TRUSTED_PROXY_HOPS)

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

START_TIME = time.time()

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

RATE_BUCKETS: dict[str, list[float]] = {}
RATE_LOCK = threading.Lock()

COOKIES_PATH: Path | None = None
JS_RUNTIME: str | None = None
FFMPEG_PATH: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _redact(text: str) -> str:
    """Strip anything secret-ish out of a message before it leaves the process."""
    if PROXY:
        try:
            parsed = urlparse(PROXY)
            for secret in (parsed.username, parsed.password):
                if secret:
                    text = text.replace(secret, "***")
            text = text.replace(PROXY, "http://<proxy>")
        except ValueError:
            pass
    if COOKIES_PATH:
        text = text.replace(str(COOKIES_PATH), "<cookies>")
    if ACCESS_TOKEN:
        text = text.replace(ACCESS_TOKEN, "***")
    return text


def clean_error(exc: BaseException) -> str:
    """Turn a yt-dlp exception into a short, readable one-liner, sans secrets."""
    text = ANSI_RE.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return _redact(first)[:400] or exc.__class__.__name__


def supported_js_runtimes() -> dict:
    """
    yt-dlp needs a JavaScript runtime to solve YouTube's nsig challenges.
    Newer yt-dlp versions take a `js_runtimes` dict; older ones ignore it.
    """
    runtimes: dict = {}
    for name in ("deno", "node", "bun", "quickjs"):
        if shutil.which(name):
            runtimes[name] = {}
    return runtimes


def client_ip() -> str:
    """
    The client address, accounting for a trusted reverse proxy.

    Werkzeug's ProxyFix rewrote REMOTE_ADDR from the right-hand side of
    X-Forwarded-For using TRUSTED_PROXY_HOPS, so a client cannot forge its
    identity by prepending extra entries to that header. With 0 hops the header
    is ignored and this is just the peer address.
    """
    return request.remote_addr or "unknown"


def rate_limited(bucket: str, key: str, limit: int) -> bool:
    """Sliding-window limiter. Returns True when the caller should be refused."""
    if limit <= 0:
        return False
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    with RATE_LOCK:
        hits = [t for t in RATE_BUCKETS.get(f"{bucket}:{key}", []) if t > cutoff]
        if len(hits) >= limit:
            RATE_BUCKETS[f"{bucket}:{key}"] = hits
            return True
        hits.append(now)
        RATE_BUCKETS[f"{bucket}:{key}"] = hits
        # opportunistic cleanup so the dict cannot grow forever
        if len(RATE_BUCKETS) > 5000:
            for name in [n for n, v in RATE_BUCKETS.items() if not v or v[-1] < cutoff]:
                RATE_BUCKETS.pop(name, None)
    return False


def active_job_count() -> int:
    with JOBS_LOCK:
        return sum(1 for j in JOBS.values() if j.get("status") in ("queued", "downloading", "processing"))


def disk_free_mb() -> int | None:
    """Free megabytes on the media filesystem, or None if it cannot be read."""
    try:
        return shutil.disk_usage(DOWNLOAD_DIR).free // (1024 * 1024)
    except OSError:
        return None


def global_rate_limited() -> bool:
    """Optional site-wide hourly ceiling, a backstop when the per-IP key is unusable."""
    return rate_limited("global", "all", RATE_LIMIT_GLOBAL)


def ytdlp_base_opts() -> dict:
    """Options shared by the metadata and download paths."""
    opts: dict = {}
    if COOKIES_PATH and COOKIES_PATH.is_file():
        opts["cookiefile"] = str(COOKIES_PATH)
    if PROXY:
        opts["proxy"] = PROXY
    runtimes = supported_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    # The generic extractor deliberately does NOT impersonate by default
    # (yt-dlp issue #11335), which leaves direct-file links 403-ing behind
    # Cloudflare on datacenter IPs. Opt in there only - yt-dlp warns that
    # forcing impersonation globally can hurt download speed and stability.
    if GENERIC_IMPERSONATE and HAS_CURL_CFFI:
        opts["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}
    return opts


# --------------------------------------------------------------------------
# yt-dlp glue
# --------------------------------------------------------------------------


class JobCancelled(Exception):
    """Raised from a yt-dlp hook to abort a job the user asked to stop."""


def _check_cancelled(job: dict) -> None:
    event = job.get("cancel_event")
    if event is not None and event.is_set():
        raise JobCancelled()


_GUARD_WARN_LOCK = threading.Lock()
_GUARD_WARNED: set[str] = set()


def _guard_warning(message: str) -> None:
    """Emit one warning about the SSRF redirect guard not being installed."""
    print(f"WARNING: {message}", file=sys.stderr)


def _warn_guard_once(key: str, message: str) -> None:
    with _GUARD_WARN_LOCK:
        if key in _GUARD_WARNED:
            return
        _GUARD_WARNED.add(key)
    _guard_warning(message)


def ensure_urllib_guard() -> None:
    """
    Validate every redirect hop on yt-dlp's urllib handler.

    Warns by default; refuses to start when YTDL_REQUIRE_GUARD=1.
    """
    if install_urllib_redirect_guard():
        return
    message = (
        "the SSRF redirect guard could not be installed on yt-dlp's urllib "
        "handler; redirect-based SSRF protection is degraded. Update or re-pin "
        "yt-dlp, or set YTDL_REQUIRE_GUARD=1 to make this fatal."
    )
    if REQUIRE_GUARD:
        raise SystemExit(f"YTDL_REQUIRE_GUARD is set but {message}")
    _warn_guard_once("urllib", message)


def _drop_unguarded_curl_handler(ydl) -> None:
    """
    Fail closed for the impersonating handler.

    If its redirects cannot be validated, drop it so requests fall back to the
    guarded urllib handler rather than fetching through an unguarded one.
    """
    try:
        handlers = ydl._request_director.handlers
    except Exception:  # pragma: no cover - yt-dlp internals changed
        return
    if "CurlCFFI" not in handlers:
        return  # curl_cffi is not in use; the urllib path is guarded instead
    handlers.pop("CurlCFFI", None)
    _warn_guard_once(
        "curl",
        "the SSRF redirect guard could not be installed on the curl_cffi "
        "handler; browser impersonation is disabled so the request falls back "
        "to the guarded handler. Update or re-pin yt-dlp.",
    )


def make_ydl(opts: dict):
    """Build a YoutubeDL with the redirect guard installed on its handlers."""
    ydl = yt_dlp.YoutubeDL(opts)
    if not install_ydl_redirect_guard(ydl):
        _drop_unguarded_curl_handler(ydl)
    return ydl


def make_progress_hook(job: dict):
    def hook(d: dict) -> None:
        _check_cancelled(job)
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            percent = (done / total * 100) if total else None
            with JOBS_LOCK:
                job["status"] = "downloading"
                job["percent"] = round(percent, 1) if percent is not None else None
                job["speed"] = d.get("speed")
                job["eta"] = d.get("eta")
                job["downloaded_bytes"] = done
                job["total_bytes"] = total or None
        elif status == "finished":
            # download is done, ffmpeg still has to merge/convert
            with JOBS_LOCK:
                job["status"] = "processing"
                job["percent"] = 100

    return hook


def make_postprocessor_hook(job: dict):
    def hook(d: dict) -> None:
        _check_cancelled(job)
        if d.get("status") in ("started", "processing"):
            with JOBS_LOCK:
                if job["status"] not in ("error", "done"):
                    job["status"] = "processing"
                    job["postprocessor"] = d.get("postprocessor", "")

    return hook


def build_opts(job: dict, quality: str, cookie_browser: str | None) -> dict:
    outdir = DOWNLOAD_DIR / job["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        job["dir"] = str(outdir)

    opts: dict = {
        "format": QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"]),
        # relative template + `paths` keeps everything inside this job's folder
        "outtmpl": "%(title).80s.%(ext)s",
        "paths": {"home": str(outdir), "temp": str(outdir / ".part")},
        "noplaylist": True,
        # NOTE: do *not* set restrictfilenames - it forces ASCII and would mangle
        # Arabic/CJK/etc titles. yt-dlp still sanitises path separators, ".." and
        # control characters out of the title before using it as a filename.
        "trim_file_name": 80,
        "windowsfilenames": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "merge_output_format": "mp4",
        # Soft preference: pick H.264/AAC when the video offers it, so the MP4
        # plays everywhere (QuickTime, phones, TVs). Falls back to VP9/AV1 or
        # Opus automatically when H.264 isn't available at that quality.
        "format_sort": ["vcodec:h264", "acodec:aac"],
        "progress_hooks": [make_progress_hook(job)],
        "postprocessor_hooks": [make_postprocessor_hook(job)],
    }
    if MAX_FILESIZE_MB > 0:
        opts["max_filesize"] = MAX_FILESIZE_MB * 1024 * 1024
    opts.update(ytdlp_base_opts())

    if quality == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    # Reading a local browser's cookie jar only makes sense on a desktop.
    if cookie_browser and not IS_SERVER:
        opts["cookiesfrombrowser"] = (cookie_browser, None, None, None)

    return opts


def find_output_file(outdir: Path) -> Path | None:
    """Pick the real media file out of the job folder (ignores partials)."""
    if not outdir.is_dir():
        return None
    files = [
        p
        for p in outdir.iterdir()
        if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp", ".json")
    ]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[0]


# Extensions a browser can save on its own from a single-file source.
DIRECT_EXT_ALLOWLIST = {
    "mp4", "webm", "mkv", "mov", "m4v",
    "mp3", "m4a", "aac", "ogg", "opus", "wav", "flac",
}
# The subset that is audio, used to keep "audio only" honest.
DIRECT_AUDIO_EXTS = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"}
# Height cap per quality choice (mirrors the intent of QUALITY_FORMATS);
# None means no cap. "audio" is handled separately (see below).
_DIRECT_CAPS: dict[str, int | None] = {"best": None, "1080": 1080, "720": 720, "480": 480}


def _has(value) -> bool:
    return value not in (None, "none")


def direct_candidate(meta: dict, quality: str) -> dict | None:
    """
    A browser-usable direct media URL for a single-file source, else None.

    Only formats that are one already-muxed http(s) file qualify. Anything that
    needs merging (separate video/audio streams), a manifest (HLS/DASH), media
    fragments, or an MP3 transcode is refused, so the caller falls back to the
    normal server-side job.

    The candidate URL is re-validated with the same SSRF guard used for
    user-supplied links, and fails closed. The URL is a bearer-style, often
    signed and expiring credential, so it is returned to the authenticated
    caller but never logged.
    """
    formats = [
        f for f in (meta.get("formats") or [])
        if isinstance(f, dict) and f.get("url")
    ]
    if not formats:
        return None

    def usable(f: dict) -> bool:
        return (
            (f.get("protocol") or "") in ("http", "https")
            and not f.get("fragments")
            and (f.get("ext") or "").lower() in DIRECT_EXT_ALLOWLIST
        )

    def muxed(f: dict) -> bool:
        return _has(f.get("vcodec")) and _has(f.get("acodec"))

    def audio_only(f: dict) -> bool:
        return not _has(f.get("vcodec")) and _has(f.get("acodec"))

    def no_codec_meta(f: dict) -> bool:
        # A raw file link: yt-dlp reports no codec metadata for these.
        return not _has(f.get("vcodec")) and not _has(f.get("acodec"))

    def raw_direct(f: dict) -> bool:
        return no_codec_meta(f) and usable(f)

    if quality == "audio":
        # Keep "audio only" honest: a single audio file, or a metadata-less
        # direct link whose extension is itself audio.
        f = formats[0] if len(formats) == 1 and usable(formats[0]) else None
        if f is None:
            return None
        is_audio = audio_only(f) or (
            no_codec_meta(f) and (f.get("ext") or "").lower() in DIRECT_AUDIO_EXTS
        )
        if not is_audio:
            return None
        fmt = f
    else:
        cap = _DIRECT_CAPS.get(quality)
        pool = [
            f for f in formats
            if muxed(f) and usable(f) and (cap is None or (f.get("height") or 0) <= cap)
        ]
        if pool:
            fmt = max(pool, key=lambda f: f.get("height") or 0)
        elif len(formats) == 1 and raw_direct(formats[0]):
            # A direct file link (single format, no codec metadata): accept the
            # one file the source offers, whatever its real resolution.
            fmt = formats[0]
        else:
            # Needs merging, a manifest, fragments, or is video/audio-only.
            return None

    url = fmt["url"]
    if validate_url(url):
        return None
    return {
        "url": url,
        "ext": (fmt.get("ext") or "").lower(),
        "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
    }


def _cleanup_dir(path) -> None:
    """Best-effort removal of a job folder (partials, finished files, everything)."""
    shutil.rmtree(path, ignore_errors=True)


def run_job(job_id: str, url: str, quality: str, cookie_browser: str | None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:  # deleted before the thread got here
        _cleanup_dir(DOWNLOAD_DIR / job_id)
        return

    outdir = DOWNLOAD_DIR / job_id
    try:
        opts = build_opts(job, quality, cookie_browser)
        with make_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if isinstance(info, dict) and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else info

        output = find_output_file(Path(job["dir"]))
        if output is None:
            raise RuntimeError("Download finished but no file was produced.")

        with JOBS_LOCK:
            job["status"] = "done"
            job["percent"] = 100
            job["file"] = str(output)
            job["filename"] = output.name
            job["filesize"] = output.stat().st_size
            job["finished_at"] = time.time()
            if isinstance(info, dict):
                job["title"] = job.get("title") or info.get("title")
                job["duration"] = info.get("duration")
    except JobCancelled:
        # The user asked to stop, or the delete endpoint cancelled the job.
        _cleanup_dir(outdir)
        with JOBS_LOCK:
            if JOBS.get(job_id) is job:
                JOBS.pop(job_id, None)
    except Exception as exc:  # noqa: BLE001 - surface any yt-dlp failure to the UI
        # A failed job leaves nothing worth keeping on disk.
        _cleanup_dir(outdir)
        with JOBS_LOCK:
            if JOBS.get(job_id) is job:
                job["status"] = "error"
                job["error"] = clean_error(exc)
                job["finished_at"] = time.time()


def start_job(url: str, quality: str, cookie_browser: str | None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "url": url,
            "quality": quality,
            "status": "queued",
            "percent": 0,
            "error": None,
            "created_at": time.time(),
            "cancel_event": threading.Event(),
        }
    thread = threading.Thread(
        target=run_job,
        args=(job_id, url, quality, cookie_browser),
        name=f"ydl-{job_id}",
        daemon=True,
    )
    thread.start()
    return job_id


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


def drop_job(job_id: str) -> None:
    """Remove a job and delete its folder. Callers must ensure it is not active."""
    job = JOBS.pop(job_id, None)
    folder = (job or {}).get("dir") or str(DOWNLOAD_DIR / job_id)
    _cleanup_dir(folder)


def janitor() -> None:
    """Delete finished jobs' files after their TTL and clean up stale folders."""
    while True:
        time.sleep(JANITOR_INTERVAL)
        now = time.time()
        for job_id, job in list(JOBS.items()):
            finished = job.get("finished_at") or job.get("created_at")
            if job.get("status") in ("done", "error") and finished and now - finished > JOB_TTL_SECONDS:
                drop_job(job_id)
        if not SWEEP_ORPHANS:
            continue
        try:
            folders = [f for f in DOWNLOAD_DIR.iterdir() if f.is_dir()]
        except OSError:
            continue
        for folder in folders:
            # Only ever touch folders that look like a job's, never a stray file.
            if not JOB_ID_RE.match(folder.name):
                continue
            try:
                stale = now - folder.stat().st_mtime > JOB_TTL_SECONDS
            except OSError:
                continue
            if folder.name not in JOBS and stale:
                _cleanup_dir(folder)


def reset_download_dir() -> None:
    """Startup sweep: remove leftover job folders (filesystem may be stale from a crash)."""
    try:
        folders = [f for f in DOWNLOAD_DIR.iterdir() if f.is_dir()]
    except OSError:
        return
    for folder in folders:
        if JOB_ID_RE.match(folder.name):
            _cleanup_dir(folder)


def materialise_cookies() -> Path | None:
    """Resolve cookies from YTDL_COOKIES_B64 or YTDL_COOKIES_FILE, if given."""
    if COOKIES_B64:
        target = Path(os.environ.get("TMPDIR") or "/tmp") / "y-download-cookies.txt"
        try:
            target.write_bytes(base64.b64decode(COOKIES_B64))
            target.chmod(0o600)
            return target
        except Exception:  # noqa: BLE001 - a bad blob must not crash startup
            return None
    if COOKIES_FILE.is_file():
        return COOKIES_FILE
    return None


def _bootstrap() -> None:
    """One-time process init. Runs on import so it also works under gunicorn."""
    global COOKIES_PATH, JS_RUNTIME, FFMPEG_PATH
    if not ACCESS_TOKEN:
        if REQUIRE_AUTH:
            raise SystemExit(
                "YTDL_REQUIRE_AUTH is set but YTDL_ACCESS_TOKEN is empty; "
                "refusing to start an unauthenticated instance.")
        if IS_SERVER:
            print(
                "WARNING: YTDL_ACCESS_TOKEN is not set - this instance is PUBLIC "
                "and anyone with the URL can use it. Set it in your host's "
                "dashboard, or set YTDL_REQUIRE_AUTH=1 to make this fatal.",
                file=sys.stderr,
            )
    COOKIES_PATH = materialise_cookies()
    FFMPEG_PATH = shutil.which("ffmpeg")
    runtimes = supported_js_runtimes()
    JS_RUNTIME = next(iter(runtimes), None)
    # Validate redirect hops on yt-dlp's urllib handler (the curl_cffi path is
    # handled per-YoutubeDL in make_ydl). Warns, or refuses to start when
    # YTDL_REQUIRE_GUARD=1.
    ensure_urllib_guard()
    if SWEEP_ORPHANS:
        reset_download_dir()
    threading.Thread(target=janitor, name="janitor", daemon=True).start()


# --------------------------------------------------------------------------
# Request hooks
# --------------------------------------------------------------------------


@app.before_request
def require_auth():
    """Optional HTTP Basic auth. /health stays open for the platform's probes."""
    if not ACCESS_TOKEN:
        return None
    if request.path in ("/health", "/robots.txt"):
        return None
    auth = request.authorization
    supplied = auth.password if auth else ""
    if not auth or not hmac.compare_digest(supplied, ACCESS_TOKEN):
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": f'Basic realm="{APP_NAME}"'},
        )
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template(
        "index.html",
        app_name=APP_NAME,
        version=VERSION,
        qualities=QUALITY_LABELS,
        is_server=IS_SERVER,
        auth_required=bool(ACCESS_TOKEN),
        direct_enabled=DIRECT_DOWNLOAD,
        ttl_minutes=max(1, JOB_TTL_SECONDS // 60),
    )


@app.get("/health")
def health():
    free_mb = disk_free_mb()
    # A unique probe file per call: a fixed name made concurrent health checks
    # race, and one request's unlink() would 503 another's check.
    writable = False
    try:
        fd, probe = tempfile.mkstemp(prefix=".health-", dir=DOWNLOAD_DIR)
        try:
            os.close(fd)
        finally:
            os.unlink(probe)
        writable = True
    except OSError:
        writable = False

    with JOBS_LOCK:
        total = len(JOBS)
    healthy = bool(FFMPEG_PATH) and writable
    return (
        jsonify(
            ok=healthy,
            status="healthy" if healthy else "degraded",
            app=APP_NAME,
            version=VERSION,
            uptime_seconds=round(time.time() - START_TIME, 1),
            jobs={"active": active_job_count(), "tracked": total},
            checks={
                "ffmpeg": bool(FFMPEG_PATH),
                "js_runtime": JS_RUNTIME,
                "media_dir_writable": writable,
                "media_dir_free_mb": free_mb,
                "cookies_configured": bool(COOKIES_PATH and COOKIES_PATH.is_file()),
                "proxy_configured": bool(PROXY),
                "impersonation": bool(GENERIC_IMPERSONATE and HAS_CURL_CFFI),
                "auth_required": bool(ACCESS_TOKEN),
            },
        ),
        200 if healthy else 503,
    )


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.post("/info")
def info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "best").strip()
    if quality not in QUALITY_FORMATS:
        quality = "best"

    if rate_limited("info", client_ip(), RATE_LIMIT_PREVIEWS) or global_rate_limited():
        return jsonify(ok=False, error="Too many previews. Please wait a little while."), 429

    error = validate_url(url)
    if error:
        return jsonify(ok=False, error=error), 400

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "extract_flat": False,
    }
    opts.update(ytdlp_base_opts())

    try:
        with make_ydl(opts) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=clean_error(exc)), 502

    if isinstance(meta, dict) and meta.get("_type") == "playlist":
        entries = [e for e in (meta.get("entries") or []) if e]
        if entries:
            meta = entries[0]

    if not isinstance(meta, dict):
        return jsonify(ok=False, error="Could not read that link."), 502

    heights = sorted({f.get("height") for f in (meta.get("formats") or []) if f.get("height")}, reverse=True)

    # Only when the feature is on, and only for safely eligible single-file
    # sources. Always present in the response so the shape is stable; null when
    # disabled or ineligible, which tells the client to use the normal flow.
    direct = direct_candidate(meta, quality) if DIRECT_DOWNLOAD else None

    return jsonify(
        ok=True,
        title=meta.get("title"),
        uploader=meta.get("uploader") or meta.get("channel"),
        duration=meta.get("duration"),
        thumbnail=meta.get("thumbnail"),
        webpage_url=meta.get("webpage_url") or url,
        heights=heights,
        is_live=bool(meta.get("is_live")),
        direct=direct,
    )


@app.post("/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "best").strip()
    cookie_browser = (data.get("cookies_browser") or "").strip().lower() or None

    # Rate limit first: validate_url() resolves DNS, which is the expensive part.
    if rate_limited("download", client_ip(), RATE_LIMIT_JOBS) or global_rate_limited():
        return jsonify(ok=False, error="Too many downloads from your address. Try again later."), 429

    error = validate_url(url)
    if error:
        return jsonify(ok=False, error=error), 400
    if quality not in QUALITY_FORMATS:
        return jsonify(ok=False, error="Unknown quality option."), 400
    if cookie_browser and cookie_browser not in ALLOWED_COOKIE_BROWSERS:
        return jsonify(ok=False, error=f"Unsupported browser: {cookie_browser}"), 400

    if active_job_count() >= MAX_CONCURRENT_JOBS:
        return jsonify(ok=False, error="The server is busy. Please try again in a moment."), 429

    free_mb = disk_free_mb()
    if MIN_FREE_MB > 0 and free_mb is not None and free_mb < MIN_FREE_MB:
        return jsonify(ok=False, error="The server is low on disk space. Try again later."), 503

    job_id = start_job(url, quality, cookie_browser)
    return jsonify(ok=True, job_id=job_id)


@app.get("/progress/<job_id>")
def progress(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(ok=False, error="Unknown or expired job."), 404
    keys = (
        "id", "status", "percent", "speed", "eta", "filename",
        "filesize", "title", "duration", "error", "postprocessor", "quality",
    )
    return jsonify(ok=True, **{k: job.get(k) for k in keys})


@app.get("/file/<job_id>")
def get_file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(ok=False, error="File is not ready."), 404
    path = Path(job["file"])
    if not path.is_file():
        return jsonify(ok=False, error="File has expired on the server."), 404
    response = send_file(
        path,
        as_attachment=True,
        download_name=job.get("filename") or path.name,
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/job/<job_id>")
def delete_job(job_id: str):
    # Only ever look up well-formed ids, so a hostile path cannot reach the store.
    if not JOB_ID_RE.match(job_id):
        return jsonify(ok=False, error="Unknown job."), 404
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(ok=False, error="Unknown job."), 404
    if job.get("status") in ("queued", "downloading", "processing"):
        # Never rmtree a folder a worker is still writing to: ask the worker to
        # stop, and let it clean up (run_job does) so the delete is race-free.
        event = job.get("cancel_event")
        if event is not None:
            event.set()
        return jsonify(ok=True, cancelled=True)
    drop_job(job_id)
    return jsonify(ok=True)


@app.errorhandler(413)
def payload_too_large(_):
    return jsonify(ok=False, error="Request body too large."), 413


@app.errorhandler(404)
def not_found(_):
    return jsonify(ok=False, error="Not found."), 404


@app.errorhandler(500)
def server_error(_):
    return jsonify(ok=False, error="Internal server error."), 500


# --------------------------------------------------------------------------
# Entrypoint (local development)
# --------------------------------------------------------------------------


def main() -> None:
    missing = []
    if not FFMPEG_PATH:
        missing.append("ffmpeg (needed to merge video+audio and convert to MP3)")
    if not JS_RUNTIME:
        missing.append("a JS runtime like node/deno (YouTube may fail without one)")
    if missing:
        print("\n  WARNING - missing optional system tools:")
        for item in missing:
            print(f"    - {item}")

    print(f"\n  {APP_NAME} v{VERSION} is running ->  http://localhost:{PORT}\n")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)


_bootstrap()

if __name__ == "__main__":
    main()
