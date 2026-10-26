#!/usr/bin/env bash
# report-viewer lifecycle smoke (2026-10-26, campus reviewer ruling B).
#
# Proves the profile form end to end, touching ONLY the report-viewer
# service (never the shared postgres/redis/minio, never 3000/8000 or the
# test database — no stack borrow needed):
#   1. --profile report up starts the viewer          -> 200
#   2. bare up -d (gate/other-session/production form) does NOT start
#      a stopped viewer                               -> connection refused
#   3. --profile report up starts it again            -> 200
#
# End state: viewer RUNNING (same as `make stack`).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f ${ROOT}/infra/docker-compose.yml"
URL="http://127.0.0.1:8200/"

up_with_profile() { ${COMPOSE} --profile report up -d report-viewer; }
up_bare()         { ${COMPOSE} up -d; }
stop_viewer()     { ${COMPOSE} stop report-viewer; }

wait_200() { # succeeds once the viewer answers 200 within 30s
  local deadline=$(( $(date +%s) + 30 ))
  until curl -sf -o /dev/null "${URL}"; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then return 1; fi
    sleep 1
  done
  return 0
}

echo "==> [1/3] --profile report up -> expect 200"
up_with_profile
if wait_200; then echo "    OK: viewer serving"; else echo "    FAIL: no 200 after profile up"; exit 1; fi

echo "==> [2/3] stop viewer, bare up -d -> expect it NOT to come back"
stop_viewer
up_bare
sleep 2
if curl -sf -o /dev/null "${URL}"; then
  echo "    FAIL: bare up started the profile service (profile semantics broken)"
  exit 1
else
  echo "    OK: bare up leaves the viewer down"
fi

echo "==> [3/3] --profile report up again -> expect 200"
up_with_profile
if wait_200; then echo "    OK: viewer back"; else echo "    FAIL: no 200 after profile up"; exit 1; fi

echo "SMOKE PASS: report viewer rides the report profile"
