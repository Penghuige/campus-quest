# backend/app/main.py
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.core.errors import register_exception_handlers
from app.core.observability import RequestIDMiddleware
from app.core.readiness import ReadinessRegistry, get_readiness_registry


def create_app() -> FastAPI:
    app = FastAPI(title="CampusQuest API")
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
