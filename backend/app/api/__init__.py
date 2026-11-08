"""FastAPI application package initialization."""
from contextlib import asynccontextmanager

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
except ImportError:
    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.routers = []
        def add_middleware(self, *args, **kwargs):
            pass
        def include_router(self, router, *args, **kwargs):
            self.routers.append(router)
    class CORSMiddleware:
        pass

from .routes import api_router
from .v1 import v1_router
from .auth import verify_startup_security_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: initialize the database, seed defaults, start the
    worker pool and its run processor, then drain it on shutdown.
    """
    # Imported lazily so that importing the API module for pure unit tests does
    # not start worker threads or touch the database.
    from ..runtime import start_runtime, stop_runtime

    start_runtime()
    try:
        yield
    finally:
        stop_runtime()


def create_app() -> FastAPI:
    verify_startup_security_config()
    
    app = FastAPI(
        title="Allowance Platform",
        version="4.2.2",
        description="Autonomous Financial Authorization Engine for AI Agents",
        lifespan=lifespan,
    )
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    app.include_router(api_router)
    app.include_router(v1_router)
    return app

app = create_app()
