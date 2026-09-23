#!/usr/bin/env bash
# scripts/verify-clean-start.sh — Plan 10 Task 10 step 2 (clean-environment gate).
#
# Proves a deployment from NOTHING on the local docker-compose stack:
#
#   1. SAFETY GUARD, then `docker compose down -v` — the guard resolves the
#      containers and volumes this exact compose file would tear down and
#      refuses to run unless the compose project is "campusquest" and every
#      matched name carries the campusquest prefix. (Standalone containers
#      on this host whose names merely START with "campusquest" are not
#      compose-managed, never appear in the resolved set, and are never
#      touched.)
#   2. `up -d` and wait for postgres/redis/minio health;
#   3. `alembic upgrade head` against the DEV database (campusquest);
#   4. seed the demo admin/teacher/students (backend/scripts/seed_demo_accounts.py);
#   5. start uvicorn (background) and poll /health/ready until 200;
#   6. start the celery worker (background) and wait for its ready banner;
#   7. start the frontend (next dev, background) and poll it until ready;
#   8. tear down every process this script started (trap).
#
# DESTRUCTIVE: step 1 deletes the compose volumes — the dev PostgreSQL
# database AND the MinIO bucket data. It never runs implicitly: the script
# requires CQ_CLEAN_START_CONFIRM=YES (or --yes) so an exploratory run
# cannot destroy dev data by accident.
#
# Required environment:
#   docker + docker compose v2, uv (backend), node/npm (frontend)
#
# Optional overrides (defaults follow infra/env.example and the spec ports
# 8000/3000 — note the compose MinIO CORS allowlist defaults to frontend
# origins localhost:3000/127.0.0.1:3000, so keep 3000 unless the compose
# environment overrides it too):
#   CQ_CLEAN_START_DEV_DATABASE_URL  default
#       postgresql+asyncpg://campusquest:campusquest-dev@localhost:15432/campusquest
#   CQ_CLEAN_START_API_PORT        default 8000
#   CQ_CLEAN_START_FRONTEND_PORT   default 3000
#   CQ_CLEAN_START_CONFIRM         must be YES (or pass --yes)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
LOG_DIR="$(mktemp -d /tmp/cq-clean-start.XXXXXX)"

DEV_DATABASE_URL="${CQ_CLEAN_START_DEV_DATABASE_URL:-postgresql+asyncpg://campusquest:campusquest-dev@localhost:15432/campusquest}"
API_PORT="${CQ_CLEAN_START_API_PORT:-8000}"
FRONTEND_PORT="${CQ_CLEAN_START_FRONTEND_PORT:-3000}"
API_ORIGIN="http://127.0.0.1:${API_PORT}"
FRONTEND_ORIGIN="http://127.0.0.1:${FRONTEND_PORT}"

# The dev-stack environment every started process shares (infra/env.example
# compose defaults: bucket `campusquest`, keys campusquest/campusquest-dev).
DEV_ENV=(
  "DATABASE_URL=${DEV_DATABASE_URL}"
  "REDIS_URL=redis://localhost:6379/0"
  "S3_ENDPOINT_URL=http://localhost:9000"
  "S3_BUCKET=campusquest"
  "S3_ACCESS_KEY=campusquest"
  "S3_SECRET_KEY=campusquest-dev"
  "BUSINESS_TIMEZONE=Asia/Shanghai"
)

PIDS=()

cleanup() {
  if [ "${#PIDS[@]}" -gt 0 ]; then
    echo "-- cleaning up started processes: ${PIDS[*]}"
    for pid in "${PIDS[@]}"; do
      # Each background process was launched through `setsid`, so it is a
      # session/group leader and -- -pid TERMs the whole tree (uvicorn,
      # celery prefork pool, next dev) at once.
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 3
    for pid in "${PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  fi
  if compgen -G "$LOG_DIR/*" >/dev/null; then
    echo "-- process logs kept in $LOG_DIR"
  else
    rmdir "$LOG_DIR"
  fi
}
trap cleanup EXIT

if [ "${1:-}" = "--yes" ]; then
  CQ_CLEAN_START_CONFIRM=YES
fi
if [ "${CQ_CLEAN_START_CONFIRM:-}" != "YES" ]; then
  echo "REFUSED: clean-start deletes the compose volumes (dev PostgreSQL" >&2
  echo "database + MinIO bucket data). Re-run with CQ_CLEAN_START_CONFIRM=YES" >&2
  echo "(or --yes) once that data is confirmed disposable." >&2
  exit 2
fi

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

wait_healthy() {
  local service="$1" timeout="${2:-120}" waited=0 state
  while :; do
    state="$(compose ps "$service" --format '{{.Health}}' | head -n 1)"
    [ "$state" = "healthy" ] && break
    if [ "$waited" -ge "$timeout" ]; then
      echo "FAIL: $service state '$state' after ${timeout}s (expected healthy)" >&2
      compose ps "$service"
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "ok: $service healthy"
}

wait_http() {
  local url="$1" timeout="${2:-120}" waited=0
  until curl -fsS -o /dev/null "$url"; do
    if [ "$waited" -ge "$timeout" ]; then
      echo "FAIL: $url not ready after ${timeout}s" >&2
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "ok: $url ready"
}

wait_worker_ready() {
  local log="$1" pid="$2" timeout="${3:-90}" waited=0
  while :; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "FAIL: celery worker exited early; last log lines:" >&2
      tail -n 40 "$log" >&2 || true
      return 1
    fi
    if grep -qE ' (ready|started)\.$' "$log"; then
      return 0
    fi
    if [ "$waited" -ge "$timeout" ]; then
      echo "FAIL: celery worker not ready after ${timeout}s; last log lines:" >&2
      tail -n 40 "$log" >&2 || true
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
}

# --- 1. Destructive-teardown guard --------------------------------------
# Resolve what THIS compose file would remove, then verify every piece
# belongs to the campusquest project before issuing down -v.
COMPOSE_PROJECT="$(awk '$1 == "name:" { print $2; exit }' "$COMPOSE_FILE")"
if [ "$COMPOSE_PROJECT" != "campusquest" ]; then
  echo "REFUSED: compose project name is '${COMPOSE_PROJECT}', expected 'campusquest'" >&2
  exit 2
fi

GUARD_VIOLATION=0
while read -r container; do
  [ -z "$container" ] && continue
  if [[ "$container" != campusquest-* ]]; then
    echo "REFUSED: compose would remove non-campusquest container '$container'" >&2
    GUARD_VIOLATION=1
  fi
done < <(compose ps -a --format '{{.Name}}')

while read -r volume; do
  [ -z "$volume" ] && continue
  if [[ "$volume" != campusquest_* ]]; then
    echo "REFUSED: compose would remove non-campusquest volume '$volume'" >&2
    GUARD_VIOLATION=1
  else
    echo "-- will delete volume: $volume"
  fi
done < <(docker volume ls --filter label=com.docker.compose.project=campusquest --format '{{.Name}}')

if [ "$GUARD_VIOLATION" -ne 0 ]; then
  exit 2
fi

echo "== verify-clean-start: tearing down the campusquest stack (volumes included) =="
compose down -v
echo "ok: down -v"

# --- 2. Dependencies up and healthy --------------------------------------
compose up -d
wait_healthy postgres
wait_healthy redis
wait_healthy minio
echo "ok: dependency stack up"

# --- 3. Migrate the dev database ------------------------------------------
(cd "$BACKEND" && env "${DEV_ENV[@]}" uv run alembic upgrade head)
echo "ok: alembic upgrade head (dev database)"

# --- 4. Seed demo accounts -------------------------------------------------
(cd "$BACKEND" && env "${DEV_ENV[@]}" uv run python scripts/seed_demo_accounts.py)
echo "ok: demo accounts seeded"

# --- 5. API (uvicorn) + readiness -----------------------------------------
(
  cd "$BACKEND" &&
    exec setsid env "${DEV_ENV[@]}" uv run uvicorn app.main:create_app --factory \
      --host 127.0.0.1 --port "$API_PORT"
) >"$LOG_DIR/api.log" 2>&1 &
API_PID=$!
PIDS+=("$API_PID")
wait_http "$API_ORIGIN/health/ready" 180
echo "ok: API ready at $API_ORIGIN"

# --- 6. Celery worker -------------------------------------------------------
(
  cd "$BACKEND" &&
    exec setsid env "${DEV_ENV[@]}" uv run celery \
      -A app.workers.celery_app:celery_app worker --loglevel=INFO
) >"$LOG_DIR/worker.log" 2>&1 &
WORKER_PID=$!
PIDS+=("$WORKER_PID")
wait_worker_ready "$LOG_DIR/worker.log" "$WORKER_PID"
if grep -qE 'Traceback|CRITICAL' "$LOG_DIR/worker.log"; then
  echo "FAIL: celery worker logged a traceback/critical:" >&2
  grep -nE 'Traceback|CRITICAL' "$LOG_DIR/worker.log" | head >&2 || true
  exit 1
fi
echo "ok: celery worker up (pid $WORKER_PID)"

# --- 7. Frontend (next dev) + readiness -------------------------------------
(
  cd "$FRONTEND" &&
    exec setsid env CQ_DEV_API_PROXY="$API_ORIGIN" \
      npx next dev -p "$FRONTEND_PORT"
) >"$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
PIDS+=("$FRONTEND_PID")
wait_http "$FRONTEND_ORIGIN" 240
echo "ok: frontend ready at $FRONTEND_ORIGIN"

echo "== verify-clean-start: PASS =="
