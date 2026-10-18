# ---------------------------------------------------------------------------
# Y Download - production image
#
# python:3.12-slim + ffmpeg + node, because yt-dlp needs both binaries:
#   ffmpeg  -> merge separate video/audio streams and convert to MP3
#   node    -> solve YouTube's nsig signature challenges
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg (merge/convert), nodejs (JS runtime for yt-dlp), curl+ca-certificates.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      nodejs \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/*

# A newer JS runtime than Debian's node. yt-dlp prefers Deno for YouTube's
# nsig/ejs challenges; fall back to node if this step fails (build stays green).
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y || true

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Wire up Deno if it landed, and keep it off PATH noise otherwise.
ENV DENO_DIR=/usr/local \
    PATH="/usr/local/bin:${PATH}" \
    YTDL_MEDIA_DIR=/tmp/y-download \
    YTDL_SWEEP_ORPHANS=1 \
    YTDL_TTL_SECONDS=1800

# Render injects PORT; 8000 is only the local default.
EXPOSE 8000

# One worker on purpose: the job registry and download threads live in-process,
# so extra workers would 404 each other's jobs. Threads handle concurrency.
CMD ["sh", "-c", "gunicorn app:app \
      --bind 0.0.0.0:${PORT:-8000} \
      --worker-class gthread \
      --workers 1 \
      --threads 8 \
      --timeout 120 \
      --graceful-timeout 30 \
      --forwarded-allow-ips '*' \
      --access-logfile - \
      --error-logfile - \
      --log-level info"]
