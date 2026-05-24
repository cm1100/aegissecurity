from __future__ import annotations

from fastapi import FastAPI

from aegis.api.routes import router
from aegis.storage import init_db


def create_app(db_url: str | None = None) -> FastAPI:
    init_db(db_url)
    app = FastAPI(
        title="Aegis Discovery",
        version="0.1.0",
        description="AI Agent discovery, correlation, risk, and policy pipeline.",
    )
    app.include_router(router)
    return app


app = create_app()
