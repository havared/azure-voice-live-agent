"""Session-based authentication for the Voice Live Agent.

Uses in-memory session tokens with HttpOnly cookies.
Credentials are configurable via the ADMIN_PASSWORD environment variable.
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Cookie, HTTPException, status

from app.config import Settings

# ── Credentials ──────────────────────────────────────────────────────
USERNAME = "admin"
PASSWORD = Settings().admin_password

# ── Session store (in-memory, resets on server restart) ──────────────
_active_sessions: set[str] = set()


def authenticate(username: str, password: str) -> Optional[str]:
    """Validate credentials and return a session token, or None."""
    if username == USERNAME and password == PASSWORD:
        token = secrets.token_urlsafe(32)
        _active_sessions.add(token)
        return token
    return None


def validate_session(session_token: Optional[str]) -> bool:
    """Check if a session token is valid."""
    return session_token is not None and session_token in _active_sessions


def invalidate_session(session_token: Optional[str]) -> None:
    """Remove a session token."""
    if session_token:
        _active_sessions.discard(session_token)


def require_auth(session_token: Optional[str] = Cookie(None)) -> str:
    """FastAPI dependency that enforces authentication.

    Raises HTTP 401 if the session cookie is missing or invalid.
    Returns the valid session token.
    """
    if not validate_session(session_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return session_token
