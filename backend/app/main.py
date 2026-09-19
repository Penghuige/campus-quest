# backend/app/main.py
from fastapi import FastAPI

def create_app() -> FastAPI:
    app = FastAPI(title="CampusQuest API")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app

app = create_app()
