# backend/app/main.py
from fastapi import FastAPI

from app.core.errors import register_exception_handlers
from app.core.observability import RequestIDMiddleware


def create_app() -> FastAPI:
    app = FastAPI(title="CampusQuest API")
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
