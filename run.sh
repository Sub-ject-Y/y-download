#!/usr/bin/env bash
#
# Y Download launcher
#
#   ./run.sh            start it in the foreground  (Ctrl+C to stop)
#   ./run.sh -d         start it in the background
#   ./run.sh stop       stop the background instance
#   ./run.sh status     is it running?
#   ./run.sh logs       follow the log
#
# Environment:
#   YTDL_PORT=8000      port to listen on
#   NO_OPEN=1           don't auto-open the browser
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PY="$DIR/.venv/bin/python"
PIDFILE="$DIR/.ytdl.pid"
LOGFILE="$DIR/ytdl.log"
PORT="${YTDL_PORT:-8000}"
URL="http://localhost:$PORT"

# Locally, never sweep away folders you downloaded by hand. Production
# (the Docker image) turns this back on.
export YTDL_SWEEP_ORPHANS="${YTDL_SWEEP_ORPHANS:-0}"

# ---------------------------------------------------------------- helpers ---

info() { printf '\033[34m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

running_pid() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # make sure the pid is still *our* app and not a recycled pid
  grep -qa "app\.py" "/proc/$pid/cmdline" 2>/dev/null || return 1
  printf '%s' "$pid"
}

ensure_venv() {
  if [ -x "$PY" ] && "$PY" -c "import flask, yt_dlp" >/dev/null 2>&1; then
    return 0
  fi
  info "First run: setting up the Python virtualenv…"
  [ -x "$PY" ] || python3 -m venv .venv
  "$DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$DIR/.venv/bin/pip" install --quiet -r requirements.txt
  info "Dependencies installed."
}

check_tools() {
  command -v ffmpeg >/dev/null 2>&1 || \
    warn "WARNING: ffmpeg not found — merging video+audio and MP3 conversion will fail.  Try: sudo pacman -S ffmpeg"
  if ! command -v node >/dev/null 2>&1 && ! command -v deno >/dev/null 2>&1; then
    warn "WARNING: no node/deno found — YouTube may fail.  Try: sudo pacman -S nodejs"
  fi
  if ! "$PY" -c "import flask" >/dev/null 2>&1; then
    die "Flask is still missing — run: ./run.sh   (it will reinstall automatically)"
  fi
}

open_browser() {
  [ "${NO_OPEN:-0}" = "1" ] && return 0
  command -v xdg-open >/dev/null 2>&1 || return 0
  ( sleep 1.5; xdg-open "$URL" >/dev/null 2>&1 || true ) &
}

# ------------------------------------------------------------------ modes ---

case "${1:-}" in
  stop)
    if pid="$(running_pid)"; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 25); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        info "Force-stopped (pid $pid)."
      else
        info "Stopped (pid $pid)."
      fi
    else
      info "Y Download is not running."
    fi
    rm -f "$PIDFILE"
    exit 0
    ;;

  status)
    if pid="$(running_pid)"; then
      info "Running (pid $pid) -> $URL"
    elif curl -sf -o /dev/null --max-time 3 "$URL/" 2>/dev/null; then
      info "Something is already serving $URL, but it wasn't started by this script."
    else
      info "Not running."
    fi
    exit 0
    ;;

  logs)
    [ -f "$LOGFILE" ] || die "No log file yet ($LOGFILE)."
    exec tail -f "$LOGFILE"
    ;;

  -d|--bg|--background)
    ensure_venv
    check_tools
    if pid="$(running_pid)"; then
      info "Already running (pid $pid) -> $URL"
      exit 0
    fi
    nohup env YTDL_PORT="$PORT" "$PY" app.py >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 1.5
    if ! running_pid >/dev/null; then
      rm -f "$PIDFILE"
      die "Failed to start. Check $LOGFILE"
    fi
    open_browser
    info "Started in the background (pid $(running_pid)) -> $URL"
    info "Stop it with: ./run.sh stop     Logs: ./run.sh logs"
    exit 0
    ;;

  ""|--fg|--foreground)
    ensure_venv
    check_tools
    if pid="$(running_pid)"; then
      info "Already running (pid $pid) -> $URL"
      exit 0
    fi
    open_browser
    info "Starting Y Download on $URL   (press Ctrl+C to stop)"
    exec env YTDL_PORT="$PORT" "$PY" app.py
    ;;

  -h|--help|help)
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
    exit 0
    ;;

  *)
    die "Unknown option: $1   (try: ./run.sh --help)"
    ;;
esac
