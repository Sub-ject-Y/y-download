"""
Gunicorn configuration for Y Download.

Used by the Docker image (and any production deployment):

    gunicorn app:app -c gunicorn.conf.py

Everything here can be overridden with the matching environment variable.
"""

import os

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

# Render (and most PaaS providers) inject PORT. Locally we fall back to 8000.
_bind_host = os.environ.get("YTDL_BIND_HOST", "0.0.0.0")
_bind_port = os.environ.get("PORT") or os.environ.get("YTDL_PORT") or "8000"
bind = f"{_bind_host}:{_bind_port}"

# Render terminates TLS at its edge and forwards the real client IP in
# X-Forwarded-For. Without this, every request would look like it came from the
# proxy and the per-IP rate limiter would be useless.
forwarded_allow_ips = "*"

# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

# ONE worker on purpose. The job registry (JOBS) and the yt-dlp download threads
# live inside the process, so a second worker would not know about jobs started
# by the first and /progress/<id> would 404. Concurrency comes from threads.
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("YTDL_THREADS", "8"))

# Downloads run in background threads while the request threads stay responsive,
# so the worker keeps sending heartbeats. This is headroom, not a hard limit on
# download length.
timeout = int(os.environ.get("YTDL_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# Don't recycle the worker: restarting it would kill in-flight downloads.
max_requests = 0

# ---------------------------------------------------------------------------
# Logging (Render captures stdout/stderr)
# ---------------------------------------------------------------------------

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("YTDL_LOG_LEVEL", "info")

# yt-dlp prints its own progress from worker threads; keep the log readable.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'
