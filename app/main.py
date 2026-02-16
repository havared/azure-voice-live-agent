"""FastAPI application for real-time speech-to-speech voice agent.

Endpoints
---------
GET  /health      Health check
GET  /            Browser test client (development)
WS   /ws/voice    WebSocket endpoint for voice sessions
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

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
    logger.info("Deployment : %s", settings.azure_openai_deployment)
    logger.info("Voice      : %s", settings.voice_name)
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


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    """Liveness / readiness probe."""
    return JSONResponse({"status": "healthy", "service": "voice-live-api"})


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


# ── Browser Test Client ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def test_client():
    """Serve a minimal browser-based test client for development."""
    return _TEST_CLIENT_HTML


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


# =====================================================================
# Embedded test client HTML (kept at bottom for readability)
# =====================================================================
_TEST_CLIENT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Voice Live – Test Client</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    max-width: 720px; margin: 40px auto; padding: 0 20px;
    background: #0f172a; color: #e2e8f0;
  }
  h1 { color: #f8fafc; font-size: 1.5rem; }
  .card {
    background: #1e293b; border-radius: 12px;
    padding: 20px; margin: 16px 0;
  }
  button {
    padding: 10px 22px; border: none; border-radius: 8px;
    font-size: 15px; font-weight: 600; cursor: pointer;
    transition: background 0.2s;
  }
  .btn-start { background: #22c55e; color: #fff; }
  .btn-start:hover { background: #16a34a; }
  .btn-stop  { background: #ef4444; color: #fff; margin-left: 8px; }
  .btn-stop:hover  { background: #dc2626; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  #status {
    font-size: 13px; color: #94a3b8;
    margin-top: 12px; min-height: 20px;
  }
  #transcript {
    max-height: 420px; overflow-y: auto;
    display: flex; flex-direction: column; gap: 6px;
  }
  .msg {
    padding: 8px 12px; border-radius: 8px;
    font-size: 14px; line-height: 1.4; word-break: break-word;
  }
  .msg-user   { background: #1d4ed8; align-self: flex-end; max-width: 80%; }
  .msg-agent  { background: #374151; align-self: flex-start; max-width: 80%; }
  .msg-status { color: #64748b; font-style: italic; font-size: 12px; align-self: center; }
</style>
</head>
<body>
<h1>Voice Live Test Client</h1>

<div class="card">
  <button id="startBtn" class="btn-start" onclick="startSession()">
    Start Session
  </button>
  <button id="stopBtn" class="btn-stop" onclick="stopSession()" disabled>
    Stop
  </button>
  <div id="status">Ready – click Start to begin</div>
</div>

<div class="card">
  <h3 style="margin:0 0 12px">Conversation</h3>
  <div id="transcript"></div>
</div>

<script>
let ws, audioCtx, playbackCtx, mediaStream, processor, nextPlayTime = 0;
let activeSources = [], isAgentSpeaking = false, playbackMuted = false;
const BARGE_IN_RMS_THRESHOLD = 0.015;

function $(id) { return document.getElementById(id); }

function setStatus(t) { $('status').textContent = t; }

function addMsg(text, cls) {
  const d = document.createElement('div');
  d.className = 'msg msg-' + cls;
  d.textContent = text;
  $('transcript').appendChild(d);
  d.scrollIntoView({ behavior: 'smooth' });
}

/* ── PCM helpers ──────────────────────────────────────────────── */
function float32ToPcm16(f32) {
  const buf = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    buf[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return buf;
}

function pcm16ToFloat32(pcm) {
  const f = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768.0;
  return f;
}

/* ── Playback scheduler ──────────────────────────────────────── */
function playChunk(arrayBuffer) {
  if (!playbackCtx || playbackMuted) return;
  const pcm = new Int16Array(arrayBuffer);
  const f32 = pcm16ToFloat32(pcm);
  const buf = playbackCtx.createBuffer(1, f32.length, 24000);
  buf.getChannelData(0).set(f32);
  const src = playbackCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playbackCtx.destination);
  const now = playbackCtx.currentTime;
  const t = Math.max(now + 0.02, nextPlayTime);
  src.start(t);
  nextPlayTime = t + buf.duration;

  activeSources.push(src);
  src.onended = () => {
    const idx = activeSources.indexOf(src);
    if (idx !== -1) activeSources.splice(idx, 1);
  };
}

function flushPlayback() {
  for (const src of activeSources) {
    try { src.stop(); } catch(e) {}
  }
  activeSources = [];
  nextPlayTime = 0;
  isAgentSpeaking = false;
  playbackMuted = true;
}

/* ── Session management ──────────────────────────────────────── */
async function startSession() {
  $('startBtn').disabled = true;
  $('stopBtn').disabled = false;
  setStatus('Connecting...');

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws/voice');
  ws.binaryType = 'arraybuffer';

  ws.onopen = async () => {
    setStatus('Connected – requesting microphone...');
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 24000, channelCount: 1,
                 echoCancellation: true, noiseSuppression: true }
      });
      audioCtx = new AudioContext({ sampleRate: 24000 });
      playbackCtx = new AudioContext({ sampleRate: 24000 });
      nextPlayTime = 0;

      const source = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = e => {
        const inputData = e.inputBuffer.getChannelData(0);
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(float32ToPcm16(inputData).buffer);
        }

        if (isAgentSpeaking) {
          let sumSq = 0;
          for (let i = 0; i < inputData.length; i++) sumSq += inputData[i] * inputData[i];
          const rms = Math.sqrt(sumSq / inputData.length);
          if (rms > BARGE_IN_RMS_THRESHOLD) {
            flushPlayback();
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'barge_in' }));
            }
          }
        }
      };
      source.connect(processor);
      processor.connect(audioCtx.destination);
      setStatus('Session active – speak now');
    } catch (err) {
      setStatus('Microphone error: ' + err.message);
      stopSession();
    }
  };

  ws.onmessage = event => {
    if (event.data instanceof ArrayBuffer) {
      playChunk(event.data);
    } else {
      const m = JSON.parse(event.data);
      if (m.type === 'session_started')
        addMsg('Session ' + m.session_id, 'status');
      else if (m.type === 'clear_playback')
        flushPlayback();
      else if (m.type === 'user_transcript')
        addMsg(m.text, 'user');
      else if (m.type === 'agent_transcript' || m.type === 'agent_text')
        addMsg(m.text, 'agent');
      else if (m.type === 'status') {
        if (m.status === 'agent_speaking') { isAgentSpeaking = true; playbackMuted = false; }
        else if (m.status === 'listening' || m.status === 'ready') isAgentSpeaking = false;
        setStatus('Status: ' + m.status);
      }
      else if (m.type === 'error')
        addMsg('Error: ' + m.message, 'status');
    }
  };

  ws.onclose = () => { setStatus('Disconnected'); cleanup(); };
  ws.onerror = () => { setStatus('WebSocket error'); };
}

function cleanup() {
  flushPlayback();
  if (processor) { processor.disconnect(); processor = null; }
  if (audioCtx)  { audioCtx.close(); audioCtx = null; }
  if (playbackCtx) { playbackCtx.close(); playbackCtx = null; }
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  $('startBtn').disabled = false;
  $('stopBtn').disabled = true;
}

function stopSession() {
  if (ws) { ws.close(); ws = null; }
  cleanup();
  setStatus('Stopped');
}
</script>
</body>
</html>
"""
