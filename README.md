# Voice Live API

Real-time speech-to-speech voice agent backend powered by [Azure AI Voice Live](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to) and [Microsoft Foundry Agent Service](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart).

A FastAPI WebSocket server that bridges browser or mobile clients to Azure Voice Live, enabling real-time conversations with an AI agent using a custom neural voice.

---

## Code Structure

```
ubp-telesales-code-samples/
├── .env.example           # Environment variable template with documentation
├── .env                   # Your actual config (git-ignored)
├── .gitignore
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
| `azure-identity` | API-key and service-principal credential helpers (no interactive login) |
| `fastapi` | Async web framework with WebSocket support |
| `uvicorn[standard]` | ASGI server (includes websockets, uvloop, httptools) |
| `pydantic-settings` | Type-safe settings loaded from environment variables |
| `python-dotenv` | Loads `.env` file into the environment |

### `app/config.py`

Centralised configuration using `pydantic-settings.BaseSettings`. Every value is loaded from the `.env` file and validated at startup. If a required variable is missing or has the wrong type, the server refuses to start with a clear error.

| Group | Variables | Notes |
|---|---|---|
| **Voice Live** | `AZURE_VOICELIVE_ENDPOINT`, `PROJECT_NAME`, `AGENT_ID`, `API_VERSION`, `API_KEY` | API key authenticates the Voice Live WebSocket connection |
| **Agent Token** | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Service principal credentials stored in `.env` -- used to programmatically mint the Foundry agent access token. **No `az login` or interactive auth required.** |
| **Custom Voice** | `AZURE_VOICELIVE_VOICE_NAME`, `VOICE_ENDPOINT_ID` | Your custom neural voice name and deployment endpoint |
| **VAD** | `VAD_THRESHOLD`, `VAD_PREFIX_PADDING_MS`, `VAD_SILENCE_DURATION_MS` | Voice activity detection tuning (sensible defaults provided) |
| **Application** | `APP_HOST`, `APP_PORT`, `LOG_LEVEL`, `ENABLE_PROACTIVE_GREETING` | Server binding and behaviour |

### `app/service.py`

Contains `VoiceLiveSessionManager` -- the core class that manages a single voice session. One instance is created per client WebSocket connection. It is single-use and follows this lifecycle:

1. **Acquire credentials** -- builds an `AzureKeyCredential` from the API key (for the Voice Live connection) and uses the service principal credentials from `.env` to programmatically mint a Foundry agent access token via `ClientSecretCredential`. No interactive login or `az login` is involved.
2. **Connect** -- opens an async WebSocket to Azure Voice Live, passing agent ID, project name, and access token as query parameters.
3. **Configure session** -- sends a `session.update` event with the custom neural voice, PCM16 audio format, server VAD, echo cancellation, and deep noise suppression.
4. **Relay loops** -- two concurrent `asyncio` tasks run until either side disconnects:
   - `client -> Voice Live`: reads binary/text frames from the client WebSocket, base64-encodes audio, and appends to the Voice Live input buffer.
   - `Voice Live -> client`: iterates server events, routes each to the appropriate handler, and forwards audio deltas (binary) and control messages (JSON) back to the client.
5. **Event handling** -- processes session lifecycle, user/agent transcripts, barge-in (cancels active responses when the user interrupts), and errors.

### `app/main.py`

FastAPI application with three endpoints:

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness / readiness probe. Returns `{"status": "healthy"}`. |
| `WebSocket` | `/ws/voice` | Real-time voice session. Accepts a WebSocket, creates a `VoiceLiveSessionManager`, and runs it for the duration of the connection. |
| `GET` | `/` | Serves an embedded browser test client (HTML + JS) for development. Captures microphone audio, streams it over the WebSocket, and plays back the agent's response. |

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
| **Voice Live WebSocket** | `AzureKeyCredential` (API key) | `AZURE_VOICELIVE_API_KEY` in `.env` |
| **Foundry Agent access token** | `ClientSecretCredential` (service principal client-credentials flow) | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` in `.env` |

The service principal token is minted programmatically on each session start. The API key and SP credentials are the only secrets required.

---

## Code Setup

### Prerequisites

- Python 3.11 (conda environment `ubp-telesales`)
- An Azure AI Foundry resource with:
  - An **API key** (Keys & Endpoint section in the portal)
  - A deployed **Foundry Agent** (created in the Agents playground)
  - A **custom neural voice** deployed via the Custom Voice portal
- A registered **Entra ID application** (service principal) with the **Cognitive Services User** role assigned on your Foundry resource. You only need the `tenant_id`, `client_id`, and `client_secret` -- these go into `.env` and are used for automated token generation. **No `az login` or interactive authentication is needed.**

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

Edit `.env` with your actual credentials. All authentication is key / secret based -- no interactive login required:

```dotenv
# Azure Voice Live (API key authenticates the WebSocket connection)
AZURE_VOICELIVE_ENDPOINT=https://your-resource.services.ai.azure.com
AZURE_VOICELIVE_PROJECT_NAME=your-project-name
AZURE_VOICELIVE_AGENT_ID=your-agent-id
AZURE_VOICELIVE_API_KEY=your-api-key          # KEY1 or KEY2 from the portal

# Service Principal (auto-generates the agent access token at runtime)
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret

# Custom Neural Voice
AZURE_VOICELIVE_VOICE_NAME=en-US-YourBrandNeural
AZURE_VOICELIVE_VOICE_ENDPOINT_ID=your-endpoint-guid
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

Open the browser test client, click **Start Session**, allow microphone access, and speak. The agent will respond through your speakers. The conversation transcript appears in real time.

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
| Binary | Raw PCM16 24 kHz mono audio bytes (agent voice) |
| Text (JSON) | `{"type": "session_started", "session_id": "..."}` |
| Text (JSON) | `{"type": "user_transcript", "text": "..."}` |
| Text (JSON) | `{"type": "agent_transcript", "text": "..."}` |
| Text (JSON) | `{"type": "agent_text", "text": "..."}` |
| Text (JSON) | `{"type": "status", "status": "listening \| processing \| agent_speaking \| ready"}` |
| Text (JSON) | `{"type": "error", "message": "..."}` |
| Text (JSON) | `{"type": "pong"}` |

---

## References

- [Voice Live Agents Quickstart (Python)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-agents-quickstart?tabs=macos%2Capi-key&pivots=programming-language-python)
- [How to use the Voice Live API](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to)
- [How to customize Voice Live input and output](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to-customize)
