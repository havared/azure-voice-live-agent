"""FastAPI application for real-time speech-to-speech voice agent.

Endpoints
---------
GET  /health      Health check
GET  /login       Login page (public)
POST /auth/login  Authenticate and get session cookie
GET  /auth/check  Check if session is valid
POST /auth/logout Invalidate session
GET  /            Voice agent client (requires auth)
WS   /ws/voice    WebSocket endpoint for voice sessions (requires auth)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Cookie, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.auth import authenticate, invalidate_session, validate_session
from app.config import Settings
from app.service import VoiceLiveSessionManager

settings = Settings()

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Voice Live API server starting")
    logger.info("Endpoint   : %s", settings.azure_openai_endpoint)
    logger.info("Model      : %s", settings.azure_model)
    logger.info("Voice      : %s (temperature=%.1f)", settings.voice_name, settings.voice_temperature)
    logger.info("VAD        : azure_semantic_vad_en (eou_timeout=%dms)", settings.eou_timeout_ms)
    yield
    logger.info("Voice Live API server shutting down")


# ── Application ──────────────────────────────────────────────────────
app = FastAPI(
    title="Voice Live API",
    description=(
        "Real-time speech-to-speech voice API powered by "
        "Azure AI Voice Live and Azure OpenAI Realtime"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static assets (CSS, JS)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    """Liveness / readiness probe."""
    return JSONResponse({"status": "healthy", "service": "voice-live-api"})


# ── Authentication ───────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/login")
async def login_page(session_token: Optional[str] = Cookie(None)):
    """Serve the login page. Redirect to / if already authenticated."""
    if validate_session(session_token):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(
        path=str(_STATIC_DIR / "login.html"),
        media_type="text/html",
    )


@app.post("/auth/login")
async def login(body: LoginRequest):
    """Authenticate and set session cookie."""
    token = authenticate(body.username, body.password)
    if token is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid username or password"},
        )
    response = JSONResponse(content={"status": "ok"})
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,  # 24 hours
        path="/",
    )
    return response


@app.get("/auth/check")
async def auth_check(session_token: Optional[str] = Cookie(None)):
    """Check if the current session is valid."""
    if validate_session(session_token):
        return JSONResponse({"authenticated": True})
    return JSONResponse(status_code=401, content={"authenticated": False})


@app.post("/auth/logout")
async def logout(session_token: Optional[str] = Cookie(None)):
    """Invalidate session and clear cookie."""
    invalidate_session(session_token)
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie(key="session_token", path="/")
    return response


# ── WebSocket ────────────────────────────────────────────────────────
@app.websocket("/ws/voice")
async def voice_websocket_endpoint(websocket: WebSocket):
    """Real-time voice session over WebSocket.

    Protocol
    --------
    Client sends:
        Binary frames  – raw PCM16 24 kHz mono audio
        Text frames    – JSON commands (optional)

    Server sends:
        Binary frames  – PCM16 24 kHz mono audio (agent voice)
        Text frames    – JSON status / transcript messages
    """
    # Check auth from cookie before accepting
    session_token = websocket.cookies.get("session_token")
    if not validate_session(session_token):
        await websocket.close(code=4401, reason="Not authenticated")
        return

    await websocket.accept()
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info("Voice session opened | client=%s", client_host)

    session = VoiceLiveSessionManager(websocket=websocket, settings=settings)

    try:
        await session.run()
    except WebSocketDisconnect:
        logger.info("Client disconnected | client=%s", client_host)
    except Exception:
        logger.exception("Unhandled error | client=%s", client_host)
    finally:
        logger.info("Voice session closed | client=%s", client_host)


# ── Browser Client (protected) ──────────────────────────────────────
@app.get("/")
async def voice_client(session_token: Optional[str] = Cookie(None)):
    """Serve the voice agent client. Requires authentication."""
    if not validate_session(session_token):
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(
        path=str(_STATIC_DIR / "index.html"),
        media_type="text/html",
    )


# ── Run directly ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=True,
    )
