# CampusQuest 01 Foundation & Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a runnable CampusQuest monorepo foundation with deterministic configuration, injectable time, PostgreSQL/Redis/S3 development services, FastAPI error contracts, SQLAlchemy/Alembic, Celery wiring, and a real integration-test harness.

**Architecture:** Backend code lives in focused domain modules under `backend/app/modules`, while cross-cutting primitives live under `backend/app/core` and database setup under `backend/app/db`. Docker Compose provides PostgreSQL, Redis, and MinIO for local/test use; tests override external adapters with fakes while PostgreSQL integration tests use a real database.

**Tech Stack:** FastAPI, Python, SQLAlchemy 2.x, Alembic, PostgreSQL, Redis, Celery, boto3-compatible S3 adapter, pytest, pytest-asyncio, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

**Required quality references:** `AGENTS.md`, `docs/quality/backend-engineering.md`, `docs/quality/quality-gates.md`, `docs/quality/agent-tooling.md`.

## Global Constraints

- PostgreSQL is the source of truth; Redis is rebuildable.
- Backend timestamps are stored as UTC.
- Natural day/month behavior uses configurable `BUSINESS_TIMEZONE`.
- Business time uses an injectable Clock.
- Stable business error codes are returned in a common envelope.
- Workers call service-layer code and do not duplicate business rules.
- Integration concurrency tests use PostgreSQL, never SQLite.

## Review Focus

1. Missing environment variables must fail fast with a readable configuration error rather than produce half-configured services.
2. Naive datetimes must never enter persisted domain state; UTC-aware timestamps are mandatory.
3. Readiness must distinguish liveness from unavailable PostgreSQL/Redis.
4. Test database cleanup must not leak rows across tests or silently point at a developer database.
5. Celery task retries must preserve request/correlation identifiers without creating a second business implementation path.

---

### Task 1: Create the Monorepo Skeleton and Backend Test Entry Point

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/unit/test_health.py`
- Create: `frontend/package.json`
- Create: `frontend/src/app/page.tsx`
- Create: `infra/env.example`
- Create: `.gitignore`

**Interfaces:**
- Consumes: none.
- Produces: `app.main:create_app() -> FastAPI`; pytest entry point; empty frontend shell.

- [ ] **Step 1: Write the failing health test**

```python
# backend/tests/unit/test_health.py
from fastapi.testclient import TestClient
from app.main import create_app

def test_live_endpoint_returns_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd backend
pytest tests/unit/test_health.py -v
```

Expected: FAIL because `app.main` or `create_app` does not exist.

- [ ] **Step 3: Add the minimal FastAPI application**

```python
# backend/app/main.py
from fastapi import FastAPI

def create_app() -> FastAPI:
    app = FastAPI(title="CampusQuest API")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app

app = create_app()
```

- [ ] **Step 4: Add package dependencies and test configuration**

`backend/pyproject.toml` must include FastAPI, uvicorn, SQLAlchemy 2.x, asyncpg, Alembic, pydantic-settings, redis, celery, boto3, argon2-cffi, pyotp, phonenumbers, httpx, pytest, pytest-asyncio, and testcontainers or Docker-backed integration-test helpers. Configure pytest to discover `tests/`.

- [ ] **Step 5: Run the test**

Run: `cd backend && pytest tests/unit/test_health.py -v`  
Expected: PASS.

- [ ] **Step 6: Create the minimal Next.js shell**

`frontend/package.json` must expose `dev`, `build`, `lint`, `typecheck`, and `test:e2e` scripts. `src/app/page.tsx` renders a static “CampusQuest” heading only.

- [ ] **Step 7: Commit**

```bash
git add backend frontend infra/env.example .gitignore
git commit -m "chore: scaffold CampusQuest monorepo"
```

### Task 2: Add Typed Configuration and Injectable Clock

**Files:**
- Create: `backend/app/core/config.py`
- Create: `backend/app/core/clock.py`
- Create: `backend/tests/unit/core/test_config.py`
- Create: `backend/tests/unit/core/test_clock.py`

**Interfaces:**
- Produces:
  - `get_settings() -> Settings`
  - `Clock.now() -> datetime`
  - `SystemClock`
  - `FrozenClock(current: datetime)`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_business_timezone_defaults_to_configured_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")
    settings = Settings()
    assert settings.business_timezone == "Asia/Shanghai"

def test_missing_database_url_fails(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 2: Write failing Clock tests**

```python
from datetime import UTC, datetime
import pytest

def test_frozen_clock_returns_exact_aware_instant():
    instant = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
    assert FrozenClock(instant).now() == instant

def test_frozen_clock_rejects_naive_datetime():
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 9, 19, 3, 0))
```

- [ ] **Step 3: Run both files and verify failure**

Run: `cd backend && pytest tests/unit/core/test_config.py tests/unit/core/test_clock.py -v`.

- [ ] **Step 4: Implement Settings**

```python
class Settings(BaseSettings):
    database_url: str
    redis_url: str
    s3_endpoint_url: str
    s3_bucket: str
    business_timezone: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    max_upload_bytes_default: int = 200 * 1024 * 1024
```

Validate `business_timezone` with `zoneinfo.ZoneInfo` at startup.

- [ ] **Step 5: Implement Clock**

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

@dataclass(frozen=True)
class FrozenClock:
    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FrozenClock requires timezone-aware datetime")

    def now(self) -> datetime:
        return self.current.astimezone(UTC)
```

- [ ] **Step 6: Run tests**

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core backend/tests/unit/core
git commit -m "feat: add typed settings and injectable clock"
```

### Task 3: Add Stable Business Errors and Request IDs

**Files:**
- Create: `backend/app/core/errors.py`
- Create: `backend/app/core/observability.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/unit/core/test_errors.py`

**Interfaces:**
- Produces:
  - `BusinessError(code: str, message: str, status_code: int, details: dict | None)`
  - error response envelope with `request_id`
  - `X-Request-ID` response header

- [ ] **Step 1: Write the failing error-envelope test**

```python
def test_business_error_has_stable_envelope():
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise BusinessError(
            code="NO_ASSIGNMENT_AVAILABLE",
            message="当前没有可领取的任务",
            status_code=409,
            details={"task_id": "abc"},
        )

    response = TestClient(app).get("/boom", headers={"X-Request-ID": "req-1"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_ASSIGNMENT_AVAILABLE"
    assert response.json()["error"]["request_id"] == "req-1"
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/unit/core/test_errors.py -v`.

- [ ] **Step 3: Implement middleware and exception handler**

Use incoming `X-Request-ID` only after length/character validation; otherwise generate a UUID. Store it on `request.state.request_id` and return it in both header and business error body.

- [ ] **Step 4: Run test and add a malformed-request-id case**

Test a 10,000-character request ID and assert the server generates a safe replacement rather than reflecting it.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/errors.py backend/app/core/observability.py backend/app/main.py backend/tests/unit/core/test_errors.py
git commit -m "feat: add request ids and stable business errors"
```

### Task 4: Add SQLAlchemy Session Management and Alembic

**Files:**
- Create: `backend/app/db/base.py`
- Create: `backend/app/db/session.py`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_foundation.py`
- Create: `backend/tests/integration/test_database.py`

**Interfaces:**
- Produces:
  - `Base`
  - `async_session_maker`
  - `get_db_session()`
  - migration pipeline.

- [ ] **Step 1: Write a real PostgreSQL integration test**

```python
@pytest.mark.integration
async def test_database_executes_select(db_session):
    value = await db_session.scalar(text("select 1"))
    assert value == 1
```

The fixture must refuse URLs whose database name does not contain an explicit test marker such as `campusquest_test`.

- [ ] **Step 2: Run test against the Compose PostgreSQL service and verify failure**

Run: `cd backend && pytest tests/integration/test_database.py -v -m integration`.

- [ ] **Step 3: Implement engine/session creation**

Use SQLAlchemy async engine with `pool_pre_ping=True`. Configure transaction rollback per test.

- [ ] **Step 4: Add Alembic foundation revision**

Create a harmless foundation revision or metadata table sufficient to prove migrations run.

- [ ] **Step 5: Verify clean database migration**

Run:

```bash
cd backend
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

Expected: all exit 0.

- [ ] **Step 6: Commit**

```bash
git add backend/app/db backend/alembic backend/alembic.ini backend/tests/integration/test_database.py
git commit -m "feat: add PostgreSQL persistence foundation"
```

### Task 5: Add Docker Compose for PostgreSQL, Redis, and MinIO

**Files:**
- Create: `infra/docker-compose.yml`
- Modify: `infra/env.example`
- Create: `backend/tests/integration/test_dependencies.py`

**Interfaces:**
- Produces local service names `postgres`, `redis`, `minio`; test bucket bootstrap.

- [ ] **Step 1: Write dependency health tests**

Test PostgreSQL `select 1`, Redis `PING`, and an S3 list-buckets/list-object operation against the configured endpoint.

- [ ] **Step 2: Create Compose services with healthchecks**

PostgreSQL uses a dedicated app database plus a test database. Redis persistence may be disabled for tests. MinIO exposes an app bucket created by an init command.

- [ ] **Step 3: Start services**

Run:

```bash
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps
```

Expected: all required services healthy.

- [ ] **Step 4: Run dependency tests**

Run: `cd backend && pytest tests/integration/test_dependencies.py -v -m integration`.

- [ ] **Step 5: Commit**

```bash
git add infra backend/tests/integration/test_dependencies.py
git commit -m "chore: add local PostgreSQL Redis and object storage"
```

### Task 6: Add Integration Adapter Protocols and Fakes

**Files:**
- Create: `backend/app/integrations/object_storage.py`
- Create: `backend/app/integrations/sms.py`
- Create: `backend/app/integrations/email.py`
- Create: `backend/tests/fakes/integrations.py`
- Create: `backend/tests/unit/integrations/test_fakes.py`

**Interfaces:**
- Produces:
  - `ObjectStorage.create_upload_url(...)`
  - `ObjectStorage.head_object(...)`
  - `ObjectStorage.create_download_url(...)`
  - `SmsSender.send(...)`
  - `EmailSender.send(...)`

- [ ] **Step 1: Write tests for deterministic fakes**

```python
def test_fake_sms_records_exact_delivery():
    sms = FakeSmsSender()
    sms.send(to="+8613800000000", template="deadline_4h", variables={"task": "T"})
    assert sms.messages == [
        SentSms(to="+8613800000000", template="deadline_4h", variables={"task": "T"})
    ]
```

Write equivalent tests for email and object storage.

- [ ] **Step 2: Run tests and verify failure**

- [ ] **Step 3: Implement Protocol interfaces and in-memory fakes**

Do not implement provider-specific business logic in domain modules.

- [ ] **Step 4: Run tests**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/integrations backend/tests/fakes backend/tests/unit/integrations
git commit -m "feat: add external service adapter contracts"
```

### Task 7: Add Celery Wiring and Service-Layer Worker Pattern

**Files:**
- Create: `backend/app/workers/celery_app.py`
- Create: `backend/app/workers/jobs/health.py`
- Create: `backend/tests/workers/test_celery_wiring.py`

**Interfaces:**
- Produces Celery app and a pattern where jobs construct dependencies then call application services.

- [ ] **Step 1: Write an eager-mode worker test**

```python
def test_worker_health_job_runs_once(celery_app):
    result = health_job.delay("req-123").get(timeout=5)
    assert result == {"ok": True, "request_id": "req-123"}
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement Celery configuration**

Use Redis as broker/result backend for local development. Make eager mode test-only. Configure JSON serialization only.

- [ ] **Step 4: Verify retry-safe dependency construction**

The worker module may import service factories, but must not contain SQL that changes domain state directly.

- [ ] **Step 5: Run worker test and commit**

```bash
git add backend/app/workers backend/tests/workers
git commit -m "feat: add Celery worker foundation"
```

### Task 8: Add Readiness Checks and Unified Verification Commands

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/app/core/readiness.py`
- Create: `backend/tests/integration/test_readiness.py`
- Modify: `backend/pyproject.toml`
- Modify: `frontend/package.json`
- Create: `Makefile`

**Interfaces:**
- Produces `GET /health/ready`; top-level commands `make test-backend`, `make test-integration`, `make verify`.

- [ ] **Step 1: Write readiness tests**

When PostgreSQL and Redis are available, expect 200. When a dependency fake reports unavailable, expect 503 with a machine-readable component list.

- [ ] **Step 2: Implement readiness without coupling liveness**

`/health/live` must remain 200 even if Redis is down; `/health/ready` reflects dependency readiness.

- [ ] **Step 3: Add Makefile verification targets**

At minimum:

```make
test-backend:
	cd backend && pytest tests/unit tests/workers -v

test-integration:
	cd backend && pytest tests/integration -v -m integration

verify:
	cd backend && pytest -v
	cd frontend && npm run typecheck && npm run lint
```

- [ ] **Step 4: Run foundation gate**

```bash
make test-backend
make test-integration
cd backend && alembic downgrade base && alembic upgrade head
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend frontend Makefile
git commit -m "chore: add readiness and verification commands"
```
