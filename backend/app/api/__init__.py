"""FastAPI application package initialization."""
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
from .auth import verify_startup_security_config

def create_app() -> FastAPI:
    verify_startup_security_config()
    
    app = FastAPI(
        title="Allowance Platform",
        version="4.2.2",
        description="Autonomous Financial Authorization Engine for AI Agents"
    )
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    app.include_router(api_router)
    return app

app = create_app()
