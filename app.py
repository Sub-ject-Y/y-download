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
import ipaddress
import os
import re
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file

import yt_dlp

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
RATE_LIMIT_WINDOW = 3600

# Optional HTTP Basic auth. Empty (the default) means the app is open.
ACCESS_TOKEN = os.environ.get("YTDL_ACCESS_TOKEN") or ""
ACCESS_USER = os.environ.get("YTDL_USERNAME") or "y-download"

# Optional cookies / proxy, needed when YouTube blocks the host's datacenter IP.
PROXY = os.environ.get("YTDL_PROXY") or ""
COOKIES_B64 = os.environ.get("YTDL_COOKIES_B64") or ""
COOKIES_FILE = Path(os.environ.get("YTDL_COOKIES_FILE") or (BASE_DIR / ".cookies.txt"))

# The quality choices offered in the UI -> yt-dlp format selector strings.
QUALITY_FORMATS: dict[str, str] = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
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

app = Flask(__name__)

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


def clean_error(exc: BaseException) -> str:
    """Turn a yt-dlp exception into a short, readable one-liner."""
    text = ANSI_RE.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first[:400] or exc.__class__.__name__


def _host_is_public(host: str) -> bool:
    """
    Reject loopback / private / link-local targets.

    Without this, /download becomes a server-side request forgery hole: yt-dlp's
    generic extractor will happily fetch http://169.254.169.254/ (cloud metadata)
    or any service reachable from inside the host's network.
    """
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False

    def resolve() -> list[str] | None:
        try:
            return [info[4][0] for info in socket.getaddrinfo(host, None)]
        except (socket.gaierror, OSError):
            return None

    box: list[list[str] | None] = []
    worker = threading.Thread(target=lambda: box.append(resolve()), daemon=True)
    worker.start()
    worker.join(timeout=4)
    if not box:
        return False  # resolution hung - fail closed

    addresses = box[0]
    if not addresses:
        # Could not resolve at all. Let yt-dlp produce the real error message.
        return True

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def validate_url(url: str) -> str | None:
    """Return an error message, or None when the url looks usable."""
    if not url:
        return "Please paste a video link."
    if len(url) > 2048:
        return "That link is too long."
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// links are supported."
    if not parsed.netloc:
        return "That doesn't look like a valid link."
    if not _host_is_public(parsed.hostname or ""):
        return "That host is not allowed."
    return None


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
    Behind Render's proxy `--forwarded-allow-ips` has already rewritten
    REMOTE_ADDR from X-Forwarded-For, so this is normally the real client.
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
    return opts


# --------------------------------------------------------------------------
# yt-dlp glue
# --------------------------------------------------------------------------


def make_progress_hook(job: dict):
    def hook(d: dict) -> None:
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


def run_job(job_id: str, url: str, quality: str, cookie_browser: str | None) -> None:
    job = JOBS[job_id]
    try:
        opts = build_opts(job, quality, cookie_browser)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if isinstance(info, dict) and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else info

        outdir = Path(job["dir"])
        output = find_output_file(outdir)
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
    except Exception as exc:  # noqa: BLE001 - surface any yt-dlp failure to the UI
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = clean_error(exc)


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
    job = JOBS.pop(job_id, None)
    folder = (job or {}).get("dir") or str(DOWNLOAD_DIR / job_id)
    shutil.rmtree(folder, ignore_errors=True)


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
            if folder.name == ".part":
                continue
            try:
                stale = now - folder.stat().st_mtime > JOB_TTL_SECONDS
            except OSError:
                continue
            if folder.name not in JOBS and stale:
                shutil.rmtree(folder, ignore_errors=True)


def reset_download_dir() -> None:
    for folder in DOWNLOAD_DIR.iterdir():
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)


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
    COOKIES_PATH = materialise_cookies()
    FFMPEG_PATH = shutil.which("ffmpeg")
    runtimes = supported_js_runtimes()
    JS_RUNTIME = next(iter(runtimes), None)
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
    if not auth or auth.password != ACCESS_TOKEN:
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
        ttl_minutes=max(1, JOB_TTL_SECONDS // 60),
    )


@app.get("/health")
def health():
    free_mb = None
    writable = False
    try:
        free_mb = round(shutil.disk_usage(DOWNLOAD_DIR).free / (1024 * 1024))
        probe = DOWNLOAD_DIR / ".health"
        probe.touch()
        probe.unlink()
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

    if rate_limited("info", client_ip(), RATE_LIMIT_PREVIEWS):
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
        with yt_dlp.YoutubeDL(opts) as ydl:
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

    return jsonify(
        ok=True,
        title=meta.get("title"),
        uploader=meta.get("uploader") or meta.get("channel"),
        duration=meta.get("duration"),
        thumbnail=meta.get("thumbnail"),
        webpage_url=meta.get("webpage_url") or url,
        heights=heights,
        is_live=bool(meta.get("is_live")),
    )


@app.post("/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "best").strip()
    cookie_browser = (data.get("cookies_browser") or "").strip().lower() or None

    error = validate_url(url)
    if error:
        return jsonify(ok=False, error=error), 400
    if quality not in QUALITY_FORMATS:
        return jsonify(ok=False, error="Unknown quality option."), 400
    if cookie_browser and cookie_browser not in ALLOWED_COOKIE_BROWSERS:
        return jsonify(ok=False, error=f"Unsupported browser: {cookie_browser}"), 400

    if rate_limited("download", client_ip(), RATE_LIMIT_JOBS):
        return jsonify(ok=False, error="Too many downloads from your address. Try again later."), 429

    if active_job_count() >= MAX_CONCURRENT_JOBS:
        return jsonify(ok=False, error="The server is busy. Please try again in a moment."), 429

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
    if job_id not in JOBS:
        return jsonify(ok=False, error="Unknown job."), 404
    drop_job(job_id)
    return jsonify(ok=True)


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api"):
        return jsonify(ok=False, error="Not found."), 404
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
