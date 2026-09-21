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
from app.modules.identity import (
    auth_router as identity_auth_router,
)
from app.modules.identity import (
    profile_router as identity_profile_router,
)
from app.modules.identity import routing_common as identity_routing_common
from app.modules.identity import (
    staff_router as identity_staff_router,
)
from app.modules.identity.dependencies import get_actor
from app.modules.points import router as points_router
from app.modules.rankings import router as rankings_router
from app.modules.submissions import router as submissions_router
from app.modules.tasks import router as tasks_router
from app.workers.celery_app import get_celery_app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bind the shared Celery app in the API process: nothing else here
    # constructs one, so without this call `shared_task` proxies (future
    # modules enqueue deadline/validation jobs from request handlers) would
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

    # Identity API: typed-exception envelope handlers first, then the
    # auth/profile/staff routers under the spec §28 prefix (the split of
    # the former single identity router; URL surface unchanged).
    identity_routing_common.register_identity_exception_handlers(app)
    app.include_router(identity_auth_router.router, prefix="/api/v1")
    app.include_router(identity_profile_router.router, prefix="/api/v1")
    app.include_router(identity_staff_router.router, prefix="/api/v1")

    # Tasks/claims API: its typed exceptions subclass BusinessError
    # (rendered by the core handler), so only the endpoint-limiter mapping
    # needs registering before the mount.
    tasks_router.register_tasks_exception_handlers(app)
    app.include_router(tasks_router.router, prefix="/api/v1")

    # Submissions/review API: same posture — BusinessError subclasses
    # render through the core handler, the endpoint-limiter mapping
    # registers here (identical render, last-writer-wins is harmless).
    submissions_router.register_submissions_exception_handlers(app)
    app.include_router(submissions_router.router, prefix="/api/v1")

    # Points/rewards + rankings/growth APIs: every typed exception in
    # both modules subclasses BusinessError with its frozen code/status,
    # so the core envelope handler alone covers them — no module-local
    # handler registration, no rate-limit mapping (spec §33.1 names no
    # bucket for these surfaces in V1).
    app.include_router(points_router.router, prefix="/api/v1")
    app.include_router(rankings_router.router, prefix="/api/v1")

    # Composition-root wiring for core's role-guard seam (app/core/rbac.py):
    # the identity module's actor dependency IS the bearer provider. Done
    # once, here — an unwired guard must fail loudly (500), never silently
    # as "deny everyone". `require_role` consumers reach the provider
    # through `get_role_bearer`.
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
