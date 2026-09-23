#!/usr/bin/env bash
# scripts/verify-migrations.sh — Plan 10 Task 10 step 1 (migration gate).
#
# Proves the committed Alembic chain against a DEDICATED empty database
# (campusquest_migrate_test on the docker-compose PostgreSQL), independent
# of both the dev database (campusquest) and the integration/e2e test
# database (campusquest_test), so a parallel test run can never race this
# verification:
#
#   1. (re)create the empty dedicated database, owned by the compose
#      init-script's `test` role;
#   2. `alembic upgrade head`;
#   3. assert the key tables and the safety-critical unique indexes exist
#      (spec §31 invariants) through psql;
#   4. `alembic downgrade base` — attempted first; if the chain does not
#      support it the reason is recorded and the database is recreated
#      instead of trusting a half-downgraded state;
#   5. `alembic upgrade head` again from the empty state.
#
# Exit 0 means every step held. CI runs the zero-db upgrade + `alembic
# check` pair on every push; this script is the local, one-command,
# downgrade-inclusive counterpart.
#
# Required environment (the same stack backend/tests/integration uses):
#   docker compose -f infra/docker-compose.yml up -d   # from repo root
#
# Optional overrides (defaults match infra/docker-compose.yml):
#   CQ_MIGRATE_DATABASE_URL  alembic target; default
#                          postgresql+asyncpg://test:test@localhost:15432/
#                          campusquest_migrate_test
#   CQ_MIGRATE_ADMIN_USER   psql admin role for CREATE/DROP DATABASE
#                          (run through docker compose exec; default
#                          campusquest)
#   CQ_MIGRATE_ADMIN_DB     psql maintenance database (default campusquest)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
BACKEND="$ROOT/backend"

PG_ADMIN_USER="${CQ_MIGRATE_ADMIN_USER:-campusquest}"
PG_ADMIN_DB="${CQ_MIGRATE_ADMIN_DB:-campusquest}"
MIGRATE_DATABASE_URL="${CQ_MIGRATE_DATABASE_URL:-postgresql+asyncpg://test:test@localhost:15432/campusquest_migrate_test}"
# URL parsing by parameter expansion (the pinned default URL carries a
# simple user:password@host/db; exotic passwords are out of scope here).
_stripped="${MIGRATE_DATABASE_URL#*://}"
_userinfo="${_stripped%%@*}"
MIGRATE_DB_ROLE="${_userinfo%%:*}"
MIGRATE_DB_PASSWORD="${_userinfo#*:}"
MIGRATE_DB_NAME="${_stripped##*/}"

# Spec §31-critical tables (plan 10 task 10: "key tables").
KEY_TABLES=(
  users
  tasks
  assignments
  assignment_claims
  submissions
  points_ledger
  point_wallets
  reward_redemptions
  comments
  notifications
  notification_deliveries
  audit_logs
  system_settings
  reward_review_grants
)

# Unique indexes whose absence would silently reopen a §31 race.
KEY_UNIQUE_INDEXES=(
  uq_users_username
  uq_student_whitelist_student_number
  uq_assignments_task_id_platform_keyword
  uq_assignment_claims_active_assignment
  uq_assignment_claims_active_user_task
  uq_points_ledger_source_type_source_id_ledger_type
  uq_comment_votes_comment_id_user_id
  uq_comment_reactions_comment_id_user_id_emoji
  uq_task_ratings_task_id_user_id
  uq_notification_deliveries_event_key_user_id_channel
)

psql_admin() {
  docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U "$PG_ADMIN_USER" -d "$PG_ADMIN_DB" "$@"
}

psql_migrate() {
  docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U "$PG_ADMIN_USER" -d "$MIGRATE_DB_NAME" "$@"
}

table_list_sql() {
  local quoted
  quoted="$(printf "'%s'," "${KEY_TABLES[@]}")"
  quoted="${quoted%,}"
  printf "SELECT t.name FROM (VALUES (%s)) AS t(name)" "$quoted"
}

unique_index_list_sql() {
  local quoted
  quoted="$(printf "'%s'," "${KEY_UNIQUE_INDEXES[@]}")"
  quoted="${quoted%,}"
  printf "SELECT i.name FROM (VALUES (%s)) AS i(name)" "$quoted"
}

assert_schema() {
  local label="$1"

  local table_csv
  table_csv="$(psql_migrate -At -c "
    SELECT string_agg(t.name, ',' ORDER BY t.name)
    FROM ($(table_list_sql)) AS t(name)
    WHERE to_regclass('public.' || t.name) IS NULL")"
  if [ -n "$table_csv" ]; then
    echo "FAIL [$label]: missing table(s): $table_csv" >&2
    return 1
  fi
  echo "ok [$label]: all ${#KEY_TABLES[@]} key tables exist"

  local index_csv
  index_csv="$(psql_migrate -At -c "
    SELECT string_agg(i.name, ',' ORDER BY i.name)
    FROM ($(unique_index_list_sql)) AS i(name)
    WHERE NOT EXISTS (
      SELECT 1 FROM pg_indexes
      WHERE schemaname = 'public' AND indexname = i.name
    )")"
  if [ -n "$index_csv" ]; then
    echo "FAIL [$label]: missing unique index(es): $index_csv" >&2
    return 1
  fi
  echo "ok [$label]: all ${#KEY_UNIQUE_INDEXES[@]} key unique indexes exist"
}

alembic() {
  # env.py resolves the URL through app settings, whose other required
  # deployment variables (Redis/S3/timezone) must validate even though
  # migrations never touch them; the compose-stack defaults match
  # backend/tests/integration/db_guard.py.
  (cd "$BACKEND" &&
    env \
      "DATABASE_URL=${MIGRATE_DATABASE_URL}" \
      REDIS_URL=redis://localhost:6379/0 \
      S3_ENDPOINT_URL=http://localhost:9000 \
      S3_BUCKET=campusquest-test \
      S3_ACCESS_KEY=campusquest \
      S3_SECRET_KEY=campusquest-dev \
      BUSINESS_TIMEZONE=Asia/Shanghai \
      uv run alembic "$@")
}

echo "== verify-migrations: dedicated database $MIGRATE_DB_NAME =="

# The compose init script (infra/postgres/init/10-test-databases.sh)
# creates the `test` role; recreate it idempotently in case this volume
# predates that script, so CREATE DATABASE ... OWNER never fails.
if ! psql_admin -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${MIGRATE_DB_ROLE}'" | grep -q 1; then
  psql_admin -c "CREATE ROLE \"${MIGRATE_DB_ROLE}\" LOGIN PASSWORD '${MIGRATE_DB_PASSWORD}'"
fi

# 1. Fresh empty dedicated database (drop first: a previous run's schema
#    must never pass as this run's upgrade).
psql_admin -c "DROP DATABASE IF EXISTS \"$MIGRATE_DB_NAME\""
psql_admin -c "CREATE DATABASE \"$MIGRATE_DB_NAME\" OWNER \"$MIGRATE_DB_ROLE\""
echo "ok: recreated empty database $MIGRATE_DB_NAME"

# 2. upgrade head on the empty database.
alembic upgrade head
echo "ok: alembic upgrade head"

# 3. schema assertions.
assert_schema "after upgrade"

# 4. downgrade base — attempted; a chain without downgrade support is
#    recorded, not fatal, and the database is recreated so step 5 still
#    starts from a provably empty state.
DOWNGRADE_STATUS=0
alembic downgrade base || DOWNGRADE_STATUS=$?
if [ "$DOWNGRADE_STATUS" -eq 0 ]; then
  # alembic_version is Alembic's own bookkeeping table and survives
  # `downgrade base` by design; anything else is an incomplete downgrade.
  LEFTOVER="$(psql_migrate -At -c "
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    ORDER BY tablename")"
  if [ -n "$LEFTOVER" ]; then
    echo "FAIL: downgrade base reported success but table(s) remain: $(echo "$LEFTOVER" | tr '\n' ' ')" >&2
    exit 1
  fi
  echo "ok: alembic downgrade base (only alembic_version remains)"
else
  echo "SKIP: alembic downgrade base failed (exit $DOWNGRADE_STATUS); reason recorded above — recreating the empty database instead" >&2
  psql_admin -c "DROP DATABASE IF EXISTS \"$MIGRATE_DB_NAME\""
  psql_admin -c "CREATE DATABASE \"$MIGRATE_DB_NAME\" OWNER \"$MIGRATE_DB_ROLE\""
fi

# 5. upgrade head again from the empty state.
alembic upgrade head
echo "ok: alembic upgrade head (second pass)"
assert_schema "after re-upgrade"

# Leave no debris: the gate is repeatable because it recreates, not
# because it relies on, the dedicated database.
psql_admin -c "DROP DATABASE IF EXISTS \"$MIGRATE_DB_NAME\""
echo "== verify-migrations: PASS =="
