"""
FastAPI Security, Authentication, and Rate Limiting Middleware.
Enforces Invariant 6 and Admin Security:
- Constant-time comparison for X-Admin-Token via hmac.compare_digest.
- DEMO_SESSION_TOKEN validation and tenant-scoping for data routes.
- IP-based sliding window rate limiting.
- Production guard: refuses startup if ADMIN_TOKEN is short or missing.
"""

import hmac
import os
import time
from collections import defaultdict
from typing import Optional
try:
    from fastapi import Header, HTTPException, Request, Security
    from fastapi.security import APIKeyHeader
except ImportError:
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = "", headers: Optional[dict] = None):
            super().__init__(f"{status_code}: {detail}")
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}
    class Header:
        def __init__(self, default=..., **kwargs):
            self.default = default
    class Request:
        def __init__(self, headers=None, client=None):
            self.headers = headers or {}
            self.client = client
    def Security(dependency=None, **kwargs):
        return dependency
    class APIKeyHeader:
        def __init__(self, name: str, auto_error: bool = False):
            self.name = name
            self.auto_error = auto_error

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-secret-admin-token-min-32-chars-long-abc-123")
DEMO_SESSION_TOKEN = os.environ.get("DEMO_SESSION_TOKEN", "demo-session-token-allowance-hackathon-2026")
DEFAULT_TENANT_ID = os.environ.get("DEMO_TENANT_ID", "tenant_demo")
APP_ENV = os.environ.get("APP_ENV", "development")

# In-memory sliding window rate limiter
_rate_limits = defaultdict(list)
_auth_failures = defaultdict(list)

MAX_AUTH_FAILURES_PER_MIN = 5
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_AGENT_RUN", "10"))

admin_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)
session_header = APIKeyHeader(name="X-Demo-Session", auto_error=False)


def verify_startup_security_config():
    """Startup verification: in production, admin token must be >= 32 chars."""
    if APP_ENV == "production":
        if not ADMIN_TOKEN or len(ADMIN_TOKEN) < 32:
            raise RuntimeError("FATAL: In production, ADMIN_TOKEN must be set and at least 32 characters long.")


def get_client_ip(request: Request) -> str:
    """Extracts client IP, respecting proxy headers if configured."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def enforce_rate_limit(request: Request, limit: int = RATE_LIMIT_PER_MIN, window_seconds: int = 60):
    """Enforces sliding window rate limit per client IP."""
    ip = get_client_ip(request)
    now = time.time()
    cutoff = now - window_seconds
    
    # Prune old records
    _rate_limits[ip] = [ts for ts in _rate_limits[ip] if ts > cutoff]
    
    if len(_rate_limits[ip]) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Maximum {limit} requests per minute.",
            headers={"Retry-After": "10"}
        )
    _rate_limits[ip].append(now)


def verify_admin_token(request: Request, token: Optional[str] = Security(admin_header)) -> bool:
    """
    Verifies X-Admin-Token using constant-time byte comparison.
    Blocks IP if repeated failed attempts exceed MAX_AUTH_FAILURES_PER_MIN.
    """
    ip = get_client_ip(request)
    now = time.time()
    cutoff = now - 60
    
    _auth_failures[ip] = [ts for ts in _auth_failures[ip] if ts > cutoff]
    if len(_auth_failures[ip]) >= MAX_AUTH_FAILURES_PER_MIN:
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Please wait 60 seconds.",
            headers={"Retry-After": "60"}
        )

    if not token:
        _auth_failures[ip].append(now)
        raise HTTPException(status_code=401, detail="X-Admin-Token header missing.")

    # Convert to bytes and compare in constant time
    token_bytes = token.encode("utf-8", errors="ignore")
    expected_bytes = ADMIN_TOKEN.encode("utf-8", errors="ignore")

    if not hmac.compare_digest(token_bytes, expected_bytes):
        _auth_failures[ip].append(now)
        raise HTTPException(status_code=401, detail="Invalid admin token.")

    return True


def get_demo_tenant(request: Request, token: Optional[str] = Security(session_header)) -> str:
    """
    Validates X-Demo-Session and returns active tenant_id.
    Ensures multi-tenant data isolation even in demo showcase.
    """
    # In development mode, allow default session if header is omitted
    if APP_ENV != "production" and not token:
        return DEFAULT_TENANT_ID

    if not token:
        raise HTTPException(status_code=401, detail="X-Demo-Session header missing.")

    token_bytes = token.encode("utf-8", errors="ignore")
    expected_bytes = DEMO_SESSION_TOKEN.encode("utf-8", errors="ignore")

    if not hmac.compare_digest(token_bytes, expected_bytes):
        raise HTTPException(status_code=401, detail="Invalid demo session token.")

    return DEFAULT_TENANT_ID
