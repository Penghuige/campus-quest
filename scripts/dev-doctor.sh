#!/usr/bin/env bash
# CampusQuest local development & runtime self-check (`make dev-doctor`).
# Catches common setup/environment pitfalls before debugging app code:
# missing tools, unedited CHANGE_ME secrets, missing MinIO mirror images,
# unapplied Alembic migrations, TOTP key vs DB volume mismatch, and
# unreachable local services.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
WARN=0
FAIL=0

ok()   { printf "  [OK]   %s\n" "$1"; PASS=$((PASS + 1)); }
warn() { printf "  [WARN] %s\n" "$1"; WARN=$((WARN + 1)); }
fail() { printf "  [FAIL] %s\n" "$1"; FAIL=$((FAIL + 1)); }

echo "=== CampusQuest Dev Doctor ==="

echo "1. Toolchain"
for bin in docker uv node npm; do
  if command -v "$bin" >/dev/null 2>&1; then
    ok "$bin ($(command -v "$bin"))"
  else
    fail "$bin not found in PATH"
  fi
done

echo "2. Backend .env"
ENV_FILE="$ROOT_DIR/backend/.env"
if [ ! -f "$ENV_FILE" ]; then
  fail "backend/.env missing (copy infra/env.example and set real dev secrets)"
else
  if grep -v '^[[:space:]]*#' "$ENV_FILE" | grep -q "CHANGE_ME"; then
    fail "backend/.env still contains CHANGE_ME placeholder(s)"
  else
    ok "backend/.env present with no CHANGE_ME placeholders"
  fi
fi

echo "3. Docker Infrastructure & MinIO Mirror Images"
if docker image inspect quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z >/dev/null 2>&1; then
  ok "MinIO server image loaded locally"
else
  warn "MinIO image missing — load from release mirror (see infra/docker-compose.yml)"
fi

for svc in campusquest-postgres-1 campusquest-redis-1 campusquest-minio-1; do
  status="$(docker inspect -f '{{.State.Status}}' "$svc" 2>/dev/null || true)"
  if [ "$status" = "running" ]; then
    ok "container $svc is running"
  else
    fail "container $svc is not running (run: cd infra && docker compose up -d)"
  fi
done

echo "4. Database Migrations & Staff TOTP Key Parity"
if [ -f "$ENV_FILE" ]; then
  (
    cd "$ROOT_DIR/backend"
    heads="$(uv run alembic heads 2>/dev/null | awk '{print $1}' | head -n 1)"
    curr="$(uv run alembic current 2>/dev/null | awk '{print $1}' | head -n 1)"
    if [ -n "$heads" ] && [ "$heads" = "$curr" ]; then
      exit 0
    else
      echo "    alembic current='${curr:-none}' heads='${heads:-unknown}'"
      exit 1
    fi
  ) && ok "Alembic schema is at head" || fail "Alembic schema is behind head (run: cd backend && uv run alembic upgrade head)"

  (
    cd "$ROOT_DIR/backend"
    uv run python - <<'PY' >/dev/null 2>&1
import asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.core.config import get_settings
from app.modules.identity.models import TotpCredential, User

async def check() -> None:
    s = get_settings()
    fernet = Fernet(s.totp_encryption_key.encode())
    engine = create_async_engine(s.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            rows = (await db.scalars(
                select(TotpCredential).join(User, User.id == TotpCredential.user_id)
                .where(User.email_normalized.in_(("admin@campus.example.edu", "teacher@campus.example.edu")))
            )).all()
            for row in rows:
                fernet.decrypt(row.secret_encrypted)
    finally:
        await engine.dispose()

asyncio.run(check())
PY
  ) && ok "Demo staff TOTP credentials decrypt cleanly with current TOTP_ENCRYPTION_KEY" \
    || fail "Demo staff TOTP decrypt failed (DB volume key mismatch with backend/.env TOTP_ENCRYPTION_KEY)"
fi

echo "5. Live HTTP Probes (optional if servers are started)"
probe() {
  local label="$1" url="$2" expected="$3"
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" 2>/dev/null)" || code="000"
  if [ "$code" = "$expected" ]; then
    ok "$label ($url -> HTTP $code)"
  else
    warn "$label ($url -> HTTP $code, expected $expected)"
  fi
}
probe "MinIO live"     "http://127.0.0.1:9000/minio/health/live" "200"
probe "FastAPI ready"  "http://127.0.0.1:8100/health/ready"      "200"
probe "Next.js login"  "http://localhost:3000/login"             "200"

echo "=== Summary: ${PASS} passed, ${WARN} warnings, ${FAIL} failed ==="
[ "$FAIL" -eq 0 ]
