# Y Download

A self-hostable web app that downloads videos with
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and hands the file straight to your
browser. Flask backend, one HTML page, no build step.

Runs locally, or as a Docker container on any host that gives you a persistent
process (Render, Railway, Fly.io, your own VPS...).

---

## Requirements

| Tool | Why | Check |
|---|---|---|
| Python 3.10+ | runs the app | `python3 --version` |
| ffmpeg | merges video+audio, converts to MP3 | `ffmpeg -version` |
| node **or** deno | solves YouTube's signature challenges | `node --version` |

`ffmpeg` and `node` are system packages, not pip packages. The JavaScript
challenge solver itself ships as a pip package (`yt-dlp-ejs`) and is installed
with the other dependencies.

`ffmpeg` and `node` are system packages, not pip packages:

```bash
# Arch / CachyOS
sudo pacman -S ffmpeg nodejs

# Debian / Ubuntu
sudo apt install ffmpeg nodejs
```

## Install & run locally

```bash
./run.sh
```

It checks your dependencies, creates the virtualenv on first run if needed, and
opens <http://localhost:8000>. Press `Ctrl+C` to stop.

Or detached from the terminal:

```bash
./run.sh -d        # start in the background
./run.sh status    # is it running?
./run.sh logs      # follow the log
./run.sh stop      # stop it
```

Change the port with `YTDL_PORT=9000 ./run.sh`, keep the browser closed with
`NO_OPEN=1 ./run.sh`.

Manual equivalent, if you prefer no script:

```bash
.venv/bin/python app.py
```

An app-menu entry called **Y Download** is installed at
`~/.local/share/applications/y-download.desktop`, so you can launch it from your
desktop's application menu. If it doesn't appear, run
`update-desktop-database ~/.local/share/applications` or log out and back in.

## Use

1. Paste a link (YouTube, Shorts, TikTok, Instagram, X, … 1000+ sites).
   A bare video id like `Kt7XVUzjVlE` is expanded to a YouTube link automatically.
2. Optionally hit **Preview** to see title, thumbnail and available resolutions.
3. Pick a **Quality**: Best MP4, 1080p, 720p, 480p, or Audio-only MP3 192k.
4. Hit **Download**. The progress bar tracks it, then the file lands in your
   browser's normal downloads. With `YTDL_DIRECT=1`, eligible single-file sources
   are saved straight from the source and never touched by the server; everything
   else uses the server-side flow.

Finished files live in `downloads/<job-id>/` and are deleted automatically after
10 minutes (configurable). A job that fails or is cancelled deletes its own
folder immediately, so partial downloads never linger.

---

## Production / hosting

### How it runs in production

```bash
gunicorn app:app -c gunicorn.conf.py
```

`gunicorn.conf.py` holds the production settings; the important one is
**`workers = 1`**. The job registry and the download threads live inside the
process, so a second worker would not know about jobs the first one is running
and `/progress` would 404. Concurrency comes from threads instead.

The app reads `PORT` from the environment (injected by Render and most PaaS
providers), falls back to `YTDL_PORT`, then to 8000. It binds `0.0.0.0` when
`PORT` is set, and `127.0.0.1` otherwise so a local run is never exposed on your
LAN.

### Docker

## 🐳 Run with Docker

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running.

### 1. Clone the repository

```bash
git clone https://github.com/Sub-Ject-Y/y-download.git
cd y-download
```

### 2. Build the Docker image

```bash
docker build -t y-download .
```

### 3. Start Y Download

```bash
docker run -d \
  --name y-download \
  -p 8000:8000 \
  -e PORT=8000 \
  y-download
```

### 4. Open the application

Visit [http://localhost:8000](http://localhost:8000) in your browser.

### Container Management

**Stop the application:**

```bash
docker stop y-download
```

**Start it again:**

```bash
docker start y-download
```

**View logs:**

```bash
docker logs -f y-download
```

Press `Ctrl+C` to exit the log viewer without stopping the container.

**Remove the container:**

```bash
docker stop y-download
docker rm y-download
```

**Note:** Downloaded files are stored according to the application's storage configuration. Files stored in temporary container storage may be lost when the container is removed.


The image is `python:3.12-slim` plus `ffmpeg` and `nodejs` (and Deno when its
installer is reachable), and writes media to `/tmp/y-download`.

### Render (free tier)

`render.yaml` is a Render Blueprint that provisions a free Docker web service
with a `/health` check.

1. Push this repo to GitHub.
2. Render dashboard → **New +** → **Blueprint** → pick the repo.
3. Render reads `render.yaml` and creates the service.

Note: `maxShutdownDelaySeconds` is rejected for free-tier services, so the
Blueprint leaves it out and Render's 30-second default applies. A redeploy or
spin-down can therefore kill a download that is still running.

Free plan caveats that matter for this app — see
[Known limitations](#known-limitations-in-production) below.

### Health check

`GET /health` returns JSON and is always exempt from auth:

```json
{
  "ok": true,
  "status": "healthy",
  "app": "Y Download",
  "version": "1.0.0",
  "uptime_seconds": 4.9,
  "jobs": { "active": 0, "tracked": 0 },
  "checks": {
    "ffmpeg": true,
    "js_runtime": "node",
    "media_dir_writable": true,
    "media_dir_free_mb": 163744,
    "cookies_configured": false,
    "proxy_configured": false,
    "auth_required": false
  }
}
```

A missing `ffmpeg` or an unwritable media directory returns **503**.

---

## Tests

The suite covers the SSRF guard (initial URLs and redirect hops), auth, the
rate-limit ordering, size/disk limits, cleanup on failure and cancellation,
concurrent `/health`, and safe job deletion. It uses only the standard library
and a temporary media directory, so it never touches `downloads/`:

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

---

## Configuration

Every setting is an environment variable. See `.env.example` for the annotated
list. Nothing is required locally.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | — | Provider-injected; setting it switches the app into server mode |
| `YTDL_PORT` | `8000` | Local port override |
| `YTDL_HOST` | `0.0.0.0` in server mode, else `127.0.0.1` | Bind address |
| `YTDL_MEDIA_DIR` | `./downloads` | Where media is written. Use `/tmp/...` on ephemeral hosts |
| `YTDL_TTL_SECONDS` | `600` | Delete finished files after N seconds |
| `YTDL_MAX_CONCURRENT_JOBS` | `3` | Downloads running at once |
| `YTDL_RATE_LIMIT_JOBS` | `10` | Downloads per IP per hour (`0` disables) |
| `YTDL_RATE_LIMIT_PREVIEWS` | `60` | `/info` calls per IP per hour |
| `YTDL_RATE_LIMIT_GLOBAL` | `0` | Site-wide hourly ceiling shared by all clients (`0` disables) |
| `YTDL_MAX_REQUEST_BYTES` | `16384` | Largest accepted JSON request body |
| `YTDL_MAX_FILESIZE_MB` | `2048` | Largest single download in MB (`0` disables) |
| `YTDL_MIN_FREE_MB` | `512` | Refuse new jobs below this much free disk (`0` disables) |
| `YTDL_TRUSTED_PROXY_HOPS` | `1` in server mode, else `0` | Proxy hops to trust in `X-Forwarded-For` (`0` ignores it) |
| `YTDL_FORWARDED_ALLOW_IPS` | `*` | Passed to gunicorn as `forwarded_allow_ips` (scheme detection) |
| `YTDL_SWEEP_ORPHANS` | `1` | Wipe unknown leftover job folders. `run.sh` sets `0` locally |
| `YTDL_ACCESS_TOKEN` | — | If set, the whole site requires HTTP Basic auth |
| `YTDL_USERNAME` | `y-download` | Basic-auth username |
| `YTDL_REQUIRE_AUTH` | `0` | If `1`, refuse to start in server mode without a token |
| `YTDL_REQUIRE_GUARD` | `0` | If `1`, refuse to start when the SSRF redirect guard can't be installed |
| `YTDL_COOKIES_B64` | — | Base64 Netscape cookie jar, for bot-checked videos |
| `YTDL_COOKIES_FILE` | `./.cookies.txt` | Path to a cookie jar instead |
| `YTDL_PROXY` | — | Outbound proxy for yt-dlp |
| `YTDL_GENERIC_IMPERSONATE` | `1` | Browser impersonation for the generic extractor (needs `curl-cffi`) |
| `YTDL_DIRECT` | `0` | If `1`, `/info` may return a direct CDN URL so the browser fetches eligible single-file sources itself |

### Secrets

No credentials are stored in the repo. `.env`, `.cookies.txt`, `downloads/` and
personal files are gitignored and excluded from the Docker build context.

---

## Known limitations in production

1. **YouTube may refuse the host's IP.** Render runs in a datacenter, and
   YouTube blocks those with *"Sign in to confirm you're not a bot"* on many
   videos. Supply cookies (`YTDL_COOKIES_B64`) exported from a logged-in browser
   and, if needed, a residential proxy (`YTDL_PROXY`). Other sites such as
   TikTok or X are far less likely to block.
2. **The free service sleeps.** Render's free web services spin down after ~15
   minutes idle; the next request takes 30–60 s to wake. A spin-down also kills
   any download in progress.
3. **The filesystem is ephemeral.** Restarting or redeploying wipes
   `/tmp/y-download`, and the in-memory job registry is lost with it, so a link
   you were polling will 404. That is exactly why the TTL cleanup exists. There
   is no persistent storage on the free plan.
4. **512 MB RAM.** ffmpeg merging a long 1080p video can get close to that. Short
   videos and 480p are comfortable.
5. **Free bandwidth is 100 GB/month** shared across the whole workspace.
6. **Downloads are synchronous per thread, not queued.** With
   `YTDL_MAX_CONCURRENT_JOBS=3`, the 4th request gets `429` instead of waiting.
7. **Rate limiting is in-process and best-effort.** The client IP is taken from
   the right-hand side of `X-Forwarded-For` using `YTDL_TRUSTED_PROXY_HOPS`, so a
   client cannot forge its identity by prepending entries, but it is only as
   trustworthy as that setting. Restarting resets counters. With auth off, the
   concurrency cap (and `YTDL_RATE_LIMIT_GLOBAL`, if set) is the real protection.
8. **Basic auth is optional and off by default.** An open instance is an open
   download proxy for anyone who finds the URL. Set `YTDL_ACCESS_TOKEN` for
   anything public (or `YTDL_REQUIRE_AUTH=1` to make its absence fatal).
   `robots.txt` disallows all crawlers as a weak extra.
9. **The SSRF guard checks every redirect hop, with a residual DNS-rebinding
   gap.** The user's URL *and* each redirect `Location` are resolved and rejected
   when they point at a private address (`netguard.py`), and DNS failures fail
   closed. The one gap left is TOCTOU: yt-dlp resolves the name again when it
   actually connects, so a name that changes its answer between the two lookups
   could still slip through. Closing that fully needs an egress proxy. If a
   future yt-dlp changes the internals the guard hooks into, startup logs a
   warning (fatal with `YTDL_REQUIRE_GUARD=1`) and browser impersonation is
   disabled so requests fall back to the guarded urllib handler.
10. **A publicly hosted downloader may breach the terms of service** of the sites
   it fetches from, and possibly of your host. The instance can be suspended.
   Keep it private or password-protected and only save content you have the right to save.
11. **Render can suspend free services that generate a lot of outbound traffic.**
    Their docs say Render "may suspend a Free web service that initiates an
    uncommonly high volume of traffic over the public internet", and a
    video downloader does exactly that on every job. This is the single biggest
    risk to a long-lived free deployment, and it is outside the app's control.
12. **Free instance hours are capped at 750 per workspace per month.** One
    always-on service is ~730 hours, so the spin-down on idle is what keeps you
    inside the budget. Exceeding it suspends your free services until the next month.
13. **Direct-to-device downloads (`YTDL_DIRECT=1`) are best-effort and
    source-dependent.** Only a single, already-muxed file with a plain `http(s)`
    URL qualifies; anything needing a video+audio merge, HLS/DASH, or an MP3
    transcode (YouTube included) still uses the server. The candidate URL is
    re-validated with the same SSRF guard and is never logged, but the browser
    fetches it cross-origin, so the saved filename cannot be controlled, the
    browser may *play* rather than save depending on the source's
    `Content-Type`, and a failure (expired, IP-bound, or header-gated URL)
    surfaces in the browser and **cannot be reliably detected by JavaScript** -
    there is no guaranteed automatic retry through the server once a navigation
    has started.

## Troubleshooting

**"Sign in to confirm you're not a bot" / "Video unavailable"**
YouTube is throttling the host. Locally, pick your browser in the **Cookies from
browser** dropdown (close that browser first on some setups). On a server, set
`YTDL_COOKIES_B64`.

**"Cloudflare anti-bot challenge" / HTTP 403**
The site is fingerprinting the server's IP. `curl-cffi` is installed and the
generic extractor impersonates Chrome by default (`YTDL_GENERIC_IMPERSONATE=1`).
If it still fails, set `YTDL_PROXY`.

**"nsig extraction failed" or only low-res formats**
No JavaScript runtime. Install `node` or `deno` and restart.

**"That host is not allowed."**
Loopback/private/link-local addresses and internal names (`.internal`, `.local`,
`.lan`, …) are blocked on purpose, so the app cannot be used to probe the host's
private network or its cloud metadata service. If DNS cannot resolve the host,
the request is refused rather than allowed through.

**"Could not resolve that host."**
The app resolves every host before fetching it and fails closed. Check the
link, or that the server's DNS is working.

**It worked yesterday and broke today**
YouTube changes constantly. Update yt-dlp:

```bash
.venv/bin/pip install -U yt-dlp
```

**Merging fails / no MP4 produced**
ffmpeg isn't on your `PATH`. Install it and restart.

**"File has expired on the server"**
Downloaded files are deleted after the TTL. Start the job again.

## Layout

```
app.py                  Flask app: routes, yt-dlp options, job store, guards
netguard.py             SSRF guard: URL checks + redirect-hop validation hooks
tests/                  unittest suite (`python -m unittest discover`)
run.sh                  local launcher: start / -d / stop / status / logs
Dockerfile              production image (python + ffmpeg + node/deno)
render.yaml             Render Blueprint (free Docker web service)
.env.example            annotated environment variables
pyproject.toml          project metadata
requirements.txt        flask, yt-dlp, gunicorn
templates/index.html
static/style.css
static/app.js
static/icon.svg         app icon (also the favicon)
downloads/              temporary media output (gitignored)
```

### HTTP API

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | The page |
| `/health` | GET | Health/readiness probe (never requires auth) |
| `/robots.txt` | GET | Disallow all crawlers |
| `/info` | POST | Metadata: title, thumbnail, duration, heights, and (when `YTDL_DIRECT=1`) a `direct` URL for eligible single-file sources |
| `/download` | POST | Start a job → `{job_id}` |
| `/progress/<job_id>` | GET | Poll status/percent/speed/eta |
| `/file/<job_id>` | GET | Stream the finished file as an attachment |
| `/job/<job_id>` | DELETE | Drop a job and delete its files |

## Note

Only download content you have the right to save. Downloading from many sites
may conflict with their terms of service.
