"""AI Assistant Platform - FastAPI entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.webhooks import router as webhooks_router
from src.api.zoom_app import router as zoom_app_router
from src.config import settings
from src.core.audit import init_db


def _setup_logging() -> None:
    """Configure root logging so application code (workflows, integrations,
    background tasks) writes to the same stream uvicorn uses for HTTP logs.

    Without this, ``logger.info("...")`` calls inside background tasks
    silently disappear because the root logger has no handlers attached.
    """
    from src.core.logging_config import setup_logging
    setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup, clean up on shutdown."""
    from src.core.scheduler import start_scheduler, stop_scheduler

    _setup_logging()
    init_db()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title=settings.app.name,
    version=settings.app.version,
    lifespan=lifespan,
)


app.include_router(webhooks_router)
app.include_router(zoom_app_router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": settings.app.version}
