#!/usr/bin/env bash
set -e

# -------- Config --------
LOG_DIR="/home/LogFiles"
APP="${APP_MODULE:-DSS:app}"           # override via App Setting APP_MODULE if not DSS:app
PORT="${WEBSITES_PORT:-8000}"          # Azure injects 8000
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LOG_DIR"
echo "[startup] $TS | APP=$APP | PORT=$PORT | PWD=$(pwd)"

# -------- Redis URL (single source: redis_url) --------
# You said you ONLY set 'redis_url' in App Settings. We honor that and export REDIS_URL too.
if [ -n "${redis_url:-}" ]; then
  export REDIS_URL="$redis_url"
elif [ -n "${REDIS_URL:-}" ]; then
  # fallback if someone set REDIS_URL instead
  :
else
  echo "[startup] WARNING: 'redis_url' not set; defaulting to redis://localhost:6379/0"
  export REDIS_URL="redis://localhost:6379/0"
fi
echo "[startup] Using REDIS_URL=${REDIS_URL}"

# -------- Graceful shutdown --------
pids=()
graceful_shutdown() {
  echo "[startup] Shutting down children: ${pids[*]}"
  for pid in "${pids[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  sleep 5
  for pid in "${pids[@]:-}"; do kill -KILL "$pid" 2>/dev/null || true; done
}
trap graceful_shutdown TERM INT

# -------- Start Gunicorn (2 web workers) --------
if [ -f "gunicorn.conf.py" ]; then
  echo "[startup] Starting Gunicorn with gunicorn.conf.py on :$PORT"
  nohup gunicorn -c gunicorn.conf.py --bind "0.0.0.0:${PORT}" "$APP" \
    >> "$LOG_DIR/gunicorn_out.log" 2>> "$LOG_DIR/gunicorn_err.log" &
else
  echo "[startup] gunicorn.conf.py not found; starting Gunicorn with defaults on :$PORT"
  nohup gunicorn --bind "0.0.0.0:${PORT}" --workers 2 --threads 8 --worker-class gthread \
    "$APP" >> "$LOG_DIR/gunicorn_out.log" 2>> "$LOG_DIR/gunicorn_err.log" &
fi
pids+=($!)

# -------- Start two background workers (bkworker.py) --------
echo "[startup] Starting bkworker #1 ..."
nohup python bkworker.py >> "$LOG_DIR/bkworker1_out.log" 2>> "$LOG_DIR/bkworker1_err.log" &
pids+=($!)

echo "[startup] Starting bkworker #2 ..."
WORKER_NAME="bkworker-2" nohup python bkworker.py >> "$LOG_DIR/bkworker2_out.log" 2>> "$LOG_DIR/bkworker2_err.log" &
pids+=($!)

echo "[startup] PIDs: ${pids[*]}"

# --- after starting processes ---
WEB_PID=${pids[0]}   # first pid we pushed was gunicorn
echo "[startup] Web PID: $WEB_PID | Worker PIDs: ${pids[@]:1}"

# Wait ONLY on the web process so the container stays up
wait "$WEB_PID" || true
code=$?
echo "[startup] Gunicorn exited with code $code; shutting down..."
graceful_shutdown
wait || true
exit "$code"

