# backend/app/main.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.core.errors import register_exception_handlers
from app.core.observability import RequestIDMiddleware
from app.core.readiness import ReadinessRegistry, get_readiness_registry
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
