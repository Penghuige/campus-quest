# backend/app/main.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.core import rbac
from app.core.errors import register_exception_handlers
from app.core.observability import RequestIDMiddleware
from app.core.readiness import ReadinessRegistry, get_readiness_registry
from app.modules.identity import router as identity_router
from app.modules.identity.dependencies import get_actor
from app.modules.tasks import router as tasks_router
from app.workers.celery_app import get_celery_app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bind the shared Celery app in the API process: nothing else here
    # constructs one, so without this call `shared_task` proxies (later
    # plans enqueue deadline/validation jobs from request handlers) would
    # resolve to Celery's fallback default app and its amqp://guest@
    # localhost broker instead of the settings-configured Redis URL.
    # Construction makes it the current app; get_celery_app also sets it
    # as the process-wide default.
    get_celery_app()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="CampusQuest API", lifespan=_lifespan)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    # Identity API (Plan 02): typed-exception envelope handlers first, then
    # the router under the spec §28 prefix.
    identity_router.register_identity_exception_handlers(app)
    app.include_router(identity_router.router, prefix="/api/v1")

    # Tasks/claims API (Plan 03): its typed exceptions subclass BusinessError
    # (rendered by the core handler), so only the endpoint-limiter mapping
    # needs registering before the mount.
    tasks_router.register_tasks_exception_handlers(app)
    app.include_router(tasks_router.router, prefix="/api/v1")

    # Composition-root wiring for core's role-guard seam (app/core/rbac.py):
    # the identity module's actor dependency IS the bearer provider. Done
    # once, here — an unwired guard must fail loudly (500), never silently
    # as "deny everyone". (The T7 integration test previewed this exact
    # wiring; `require_role` consumers reach it through `get_role_bearer`.)
    app.dependency_overrides[rbac.get_role_bearer] = get_actor

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        # Liveness never consults dependencies (spec §34): the process being
        # up is enough, so a Redis outage must not fail this endpoint.
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(
        registry: Annotated[ReadinessRegistry, Depends(get_readiness_registry)],
    ) -> JSONResponse:
        # Returned directly (not raised): unavailability is a health signal,
        # not an error, so it bypasses the §29 error envelope handlers.
        report = await registry.check()
        return JSONResponse(
            content={"status": report.status, "components": report.components},
            status_code=200 if report.status == "available" else 503,
        )

    return app


app = create_app()
