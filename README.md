# Voice Live API

Real-time speech-to-speech voice backend powered by [Azure AI Voice Live](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to) and [Azure OpenAI Realtime](https://learn.microsoft.com/en-us/azure/ai-services/openai/realtime-audio-quickstart).

A FastAPI WebSocket server that bridges browser or mobile clients to Azure Voice Live, enabling real-time conversations using an Azure OpenAI Realtime model with a standard voice.

---

## Code Structure

```
ubp-telesales-code-samples/
├── .env.example           # Environment variable template with documentation
├── .env                   # Your actual config (git-ignored)
├── .gitignore
├── agent_instructions.md  # Agent system prompt / behaviour instructions
├── requirements.txt       # Python dependencies
├── README.md
└── app/
    ├── __init__.py        # Package marker
    ├── config.py          # Typed application settings
    ├── service.py         # Voice Live session manager
    └── main.py            # FastAPI application entry point
```

### `requirements.txt`

All Python dependencies required by the project:

| Package | Purpose |
|---|---|
| `azure-ai-voicelive[aiohttp]` | Voice Live SDK with async WebSocket transport |
| `fastapi` | Async web framework with WebSocket support |
| `uvicorn[standard]` | ASGI server (includes websockets, uvloop, httptools) |
| `pydantic-settings` | Type-safe settings loaded from environment variables |
| `python-dotenv` | Loads `.env` file into the environment |

### `app/config.py`

Centralised configuration using `pydantic-settings.BaseSettings`. Every value is loaded from the `.env` file and validated at startup. If a required variable is missing or has the wrong type, the server refuses to start with a clear error.

| Group | Variables | Notes |
|---|---|---|
| **Azure OpenAI** | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_API_VERSION` | API key authenticates the Voice Live WebSocket connection |
| **Voice** | `VOICE_NAME` | Standard voice name (default: `alloy`) |
| **VAD** | `VAD_THRESHOLD`, `VAD_PREFIX_PADDING_MS`, `VAD_SILENCE_DURATION_MS` | Voice activity detection tuning (sensible defaults provided) |
| **Agent** | `AGENT_INSTRUCTIONS_FILE` | Path to the markdown file containing the agent system prompt (default: `agent_instructions.md`) |
| **Application** | `APP_HOST`, `APP_PORT`, `LOG_LEVEL`, `ENABLE_PROACTIVE_GREETING` | Server binding and behaviour |

### `agent_instructions.md`

The agent's system prompt written in plain markdown. This file is read at the start of each voice session and sent as the `instructions` field in the `session.update` event to Azure OpenAI Realtime. To change how the agent behaves, edit this file — no code changes required.

### `app/service.py`

Contains `VoiceLiveSessionManager` -- the core class that manages a single voice session. One instance is created per client WebSocket connection. It is single-use and follows this lifecycle:

1. **Connect** -- opens an async WebSocket to Azure Voice Live, passing the deployment name as a query parameter and authenticating with an API key.
2. **Configure session** -- sends a `session.update` event with the voice name, PCM16 audio format, server VAD, echo cancellation, and deep noise suppression.
3. **Relay loops** -- two concurrent `asyncio` tasks run until either side disconnects:
   - `client -> Voice Live`: reads binary/text frames from the client WebSocket, base64-encodes audio, and appends to the Voice Live input buffer.
   - `Voice Live -> client`: iterates server events, routes each to the appropriate handler, and forwards audio deltas (binary) and control messages (JSON) back to the client.
4. **Event handling** -- processes session lifecycle, user/agent transcripts, barge-in (cancels active responses when the user interrupts), and errors.

### `app/main.py`

FastAPI application with three endpoints:

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness / readiness probe. Returns `{"status": "healthy"}`. |
| `WebSocket` | `/ws/voice` | Real-time voice session. Accepts a WebSocket, creates a `VoiceLiveSessionManager`, and runs it for the duration of the connection. |
| `GET` | `/` | Serves an embedded browser test client (HTML + JS) for development. Captures microphone audio, streams it over the WebSocket, and plays back the response. |

The file also configures structured logging and a lifespan handler that logs startup configuration.

### `.env.example`

Documented template for all required environment variables. Copy to `.env` and fill in your values.

### `.gitignore`

Excludes `.env`, `__pycache__`, logs, virtual environments, and IDE files from version control.

---

## Authentication Model

All credentials are read from `.env` at startup. There is **no `az login`, no interactive browser flow, and no CLI dependency**.

| What | How | Source |
|---|---|---|
| **Voice Live WebSocket** | `AzureKeyCredential` (API key) | `AZURE_OPENAI_KEY` in `.env` |

The API key is the only secret required.

---

## Code Setup

### Prerequisites

- Python 3.11 (conda environment `ubp-telesales`)
- An Azure AI resource with:
  - An **API key** (Keys & Endpoint section in the portal)
  - A deployed **Azure OpenAI Realtime** model (e.g. `gpt-realtime`)

### 1. Clone and enter the project

```bash
cd ubp-telesales-code-samples
```

### 2. Activate the conda environment

```bash
conda activate ubp-telesales
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your actual credentials:

```dotenv
# Azure OpenAI Realtime (API key authenticates the WebSocket connection)
AZURE_OPENAI_ENDPOINT=wss://your-resource.services.ai.azure.com
AZURE_OPENAI_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT=gpt-realtime
AZURE_API_VERSION=2025-10-01

# Voice (standard OpenAI voice)
VOICE_NAME=alloy
```

### 5. Run the server

```bash
python -m app.main
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `APP_HOST` / `APP_PORT`).

### 6. Test

| What | URL |
|---|---|
| Health check | `GET http://localhost:8000/health` |
| API docs | `http://localhost:8000/docs` |
| Browser test client | `http://localhost:8000/` |

Open the browser test client, click **Start Session**, allow microphone access, and speak. The model will respond through your speakers. The conversation transcript appears in real time.

---

## WebSocket Protocol (`/ws/voice`)

### Client to Server

| Frame type | Content |
|---|---|
| Binary | Raw PCM16 24 kHz mono audio bytes |
| Text (JSON) | `{"type": "audio", "audio": "<base64>"}` (alternative to binary) |
| Text (JSON) | `{"type": "ping"}` |

### Server to Client

| Frame type | Content |
|---|---|
| Binary | Raw PCM16 24 kHz mono audio bytes (model voice) |
| Text (JSON) | `{"type": "session_started", "session_id": "..."}` |
| Text (JSON) | `{"type": "user_transcript", "text": "..."}` |
| Text (JSON) | `{"type": "agent_transcript", "text": "..."}` |
| Text (JSON) | `{"type": "agent_text", "text": "..."}` |
| Text (JSON) | `{"type": "status", "status": "listening \| processing \| agent_speaking \| ready"}` |
| Text (JSON) | `{"type": "error", "message": "..."}` |
| Text (JSON) | `{"type": "pong"}` |

---

## References

- [Voice Live Quickstart (Python)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart?tabs=macos%2Capi-key&pivots=programming-language-python)
- [How to use the Voice Live API](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to)
- [Azure OpenAI Realtime Audio Quickstart](https://learn.microsoft.com/en-us/azure/ai-services/openai/realtime-audio-quickstart)
