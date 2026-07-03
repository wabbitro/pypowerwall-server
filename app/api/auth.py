"""
Shared authentication helpers for API routers.

Control (write) operations across all routers are gated by PW_CONTROL_SECRET.
Keeping the check in one place ensures every write surface enforces the same
rules — see the "no unauthenticated writes" principle in AGENTS.md.
"""
import hmac
from typing import Optional

from fastapi import Header, HTTPException

from app.config import settings


def verify_control_token(authorization: Optional[str] = Header(None)):
    """Verify control token for authenticated operations."""
    if not settings.control_enabled or not settings.control_secret:
        raise HTTPException(status_code=403, detail="Control features not enabled")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Support both "Bearer token" and plain token
    token = (
        authorization[len("Bearer "):]
        if authorization.startswith("Bearer ")
        else authorization
    )

    if not hmac.compare_digest(token, settings.control_secret):
        raise HTTPException(status_code=401, detail="Invalid control token")

    return True
