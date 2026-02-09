"""Voice Live session manager.

Bridges a client WebSocket connection to Azure AI Voice Live for real-time
speech-to-speech voice agent interactions.

WebSocket Protocol
==================

Client -> Server
    Binary frame : Raw PCM16 24 kHz mono audio bytes
    Text frame   : JSON  {"type": "audio", "audio": "<base64 PCM16>"}

Server -> Client
    Binary frame : Raw PCM16 24 kHz mono audio bytes  (agent speech)
    Text frame   : JSON control messages
        {"type": "session_started", "session_id": "..."}
        {"type": "user_transcript", "text": "..."}
        {"type": "agent_transcript", "text": "..."}
        {"type": "agent_text", "text": "..."}
        {"type": "status", "status": "listening | processing | agent_speaking | ready"}
        {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Optional

from azure.ai.voicelive.aio import VoiceLiveConnection, connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureCustomVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
)
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import ClientSecretCredential
from fastapi import WebSocket, WebSocketDisconnect

from app.config import Settings

logger = logging.getLogger(__name__)


class VoiceLiveSessionManager:
    """Manages one Voice Live session, bridging a client WebSocket to Azure.

    Lifecycle: connect -> configure -> relay audio bidirectionally -> cleanup.
    Each instance is single-use and should not be reused across sessions.
    """

    def __init__(self, websocket: WebSocket, settings: Settings) -> None:
        self._ws = websocket
        self._settings = settings
        self._connection: Optional[VoiceLiveConnection] = None

        # Response state tracking
        self._active_response: bool = False
        self._response_api_done: bool = False
        self._conversation_started: bool = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Open a Voice Live session and relay audio until disconnect."""
        try:
            vl_credential, agent_token = await self._acquire_credentials()

            async with connect(
                endpoint=self._settings.azure_voicelive_endpoint,
                credential=vl_credential,
                query={
                    "agent-id": self._settings.azure_voicelive_agent_id,
                    "agent-project-name": self._settings.azure_voicelive_project_name,
                    "agent-access-token": agent_token,
                },
            ) as connection:
                self._connection = connection
                logger.info(
                    "Connected to Voice Live | agent=%s project=%s",
                    self._settings.azure_voicelive_agent_id,
                    self._settings.azure_voicelive_project_name,
                )

                await self._configure_session()
                await self._run_relay_loops()

        except WebSocketDisconnect:
            logger.info("Client WebSocket disconnected")
        except Exception:
            logger.exception("Voice Live session error")
            await self._send_error("Internal session error")
        finally:
            self._connection = None

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    async def _acquire_credentials(
        self,
    ) -> tuple[AzureKeyCredential, str]:
        """Return ``(voicelive_credential, agent_access_token)``.

        Uses API key for the Voice Live WebSocket connection and a service
        principal (client credentials) to mint the Foundry agent access token.
        All values come from .env -- no interactive ``az login`` required.
        """
        # 1. API key credential for the Voice Live connection
        vl_credential = AzureKeyCredential(self._settings.azure_voicelive_api_key)
        logger.info("Using API key credential for Voice Live connection")

        # 2. Agent access token via service principal (client credentials flow)
        sp_credential = ClientSecretCredential(
            tenant_id=self._settings.azure_tenant_id,
            client_id=self._settings.azure_client_id,
            client_secret=self._settings.azure_client_secret,
        )
        try:
            token = await sp_credential.get_token("https://ai.azure.com/.default")
            agent_token = token.token
            logger.info("Acquired agent access token via service principal")
        finally:
            await sp_credential.close()

        return vl_credential, agent_token

    # ------------------------------------------------------------------
    # Session configuration
    # ------------------------------------------------------------------

    async def _configure_session(self) -> None:
        """Send ``session.update`` with voice, VAD, echo-cancel & noise-reduce."""
        assert self._connection is not None

        voice_config = self._build_voice_config()

        turn_detection = ServerVad(
            threshold=self._settings.vad_threshold,
            prefix_padding_ms=self._settings.vad_prefix_padding_ms,
            silence_duration_ms=self._settings.vad_silence_duration_ms,
        )

        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            voice=voice_config,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=turn_detection,
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
        )

        await self._connection.session.update(session=session_config)
        logger.info("Session configuration sent")

    def _build_voice_config(self) -> AzureCustomVoice:
        """Build the custom neural voice configuration from settings."""
        return AzureCustomVoice(
            name=self._settings.azure_voicelive_voice_name,
            endpoint_id=self._settings.azure_voicelive_voice_endpoint_id,
        )

    # ------------------------------------------------------------------
    # Relay loops
    # ------------------------------------------------------------------

    async def _run_relay_loops(self) -> None:
        """Run client->VL and VL->client tasks; cancel peer on first exit."""
        client_task = asyncio.create_task(
            self._relay_client_to_voicelive(), name="client-to-vl"
        )
        events_task = asyncio.create_task(
            self._relay_voicelive_to_client(), name="vl-to-client"
        )
        tasks = [client_task, events_task]

        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            # Cancel the surviving task
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            # Re-raise real exceptions from completed tasks
            for task in done:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _relay_client_to_voicelive(self) -> None:
        """Forward audio from the client WebSocket to Voice Live."""
        assert self._connection is not None

        while True:
            try:
                data = await self._ws.receive()
            except WebSocketDisconnect:
                logger.info("Client disconnected during audio relay")
                return

            ws_type = data.get("type", "")
            if ws_type == "websocket.disconnect":
                return

            # ── Binary frame: raw PCM16 audio ───────────────────────
            raw_bytes: Optional[bytes] = data.get("bytes")
            if raw_bytes:
                audio_b64 = base64.b64encode(raw_bytes).decode("utf-8")
                await self._connection.input_audio_buffer.append(audio=audio_b64)
                continue

            # ── Text frame: JSON command ─────────────────────────────
            text: Optional[str] = data.get("text")
            if text:
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from client: %.100s", text)
                    continue

                msg_type = msg.get("type", "")
                if msg_type == "audio":
                    audio_b64 = msg.get("audio", "")
                    if audio_b64:
                        await self._connection.input_audio_buffer.append(
                            audio=audio_b64
                        )
                elif msg_type == "ping":
                    await self._send_json({"type": "pong"})

    async def _relay_voicelive_to_client(self) -> None:
        """Iterate Voice Live events and forward to the client WebSocket."""
        assert self._connection is not None

        async for event in self._connection:
            try:
                await self._handle_event(event)
            except (asyncio.CancelledError, WebSocketDisconnect):
                raise
            except Exception:
                logger.exception(
                    "Error handling event %s",
                    getattr(event, "type", "unknown"),
                )

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _handle_event(self, event: Any) -> None:  # noqa: C901
        """Route and process a single Voice Live server event."""
        assert self._connection is not None
        event_type = event.type
        logger.debug("Event: %s", event_type)

        # ── Session ready ────────────────────────────────────────────
        if event_type == ServerEventType.SESSION_UPDATED:
            session_id = getattr(event.session, "id", "unknown")
            logger.info("Session ready: %s", session_id)
            await self._send_json(
                {"type": "session_started", "session_id": session_id}
            )

            if (
                self._settings.enable_proactive_greeting
                and not self._conversation_started
            ):
                self._conversation_started = True
                try:
                    await self._connection.response.create()
                    logger.info("Proactive greeting requested")
                except Exception:
                    logger.exception("Failed to request proactive greeting")

        # ── User transcript ──────────────────────────────────────────
        elif (
            event_type
            == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
        ):
            transcript = event.get("transcript", "")
            logger.info("User: %s", transcript)
            await self._send_json({"type": "user_transcript", "text": transcript})

        # ── Agent text response ──────────────────────────────────────
        elif event_type == ServerEventType.RESPONSE_TEXT_DONE:
            text = event.get("text", "")
            logger.info("Agent text: %s", text)
            await self._send_json({"type": "agent_text", "text": text})

        # ── Agent audio transcript ───────────────────────────────────
        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            transcript = event.get("transcript", "")
            logger.info("Agent transcript: %s", transcript)
            await self._send_json({"type": "agent_transcript", "text": transcript})

        # ── User started speaking (potential barge-in) ───────────────
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            logger.info("Speech started")
            await self._send_json({"type": "status", "status": "listening"})

            if self._active_response and not self._response_api_done:
                try:
                    await self._connection.response.cancel()
                    logger.info("Cancelled active response (barge-in)")
                except Exception as exc:
                    msg = str(exc).lower()
                    if "no active response" in msg:
                        logger.debug("Cancel ignored - already completed")
                    else:
                        logger.warning("Cancel failed: %s", exc)

        # ── User stopped speaking ────────────────────────────────────
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("Speech stopped")
            await self._send_json({"type": "status", "status": "processing"})

        # ── Response lifecycle ───────────────────────────────────────
        elif event_type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_api_done = False
            await self._send_json({"type": "status", "status": "agent_speaking"})

        elif event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
            audio_data = event.delta
            if audio_data:
                if isinstance(audio_data, bytes):
                    await self._ws.send_bytes(audio_data)
                else:
                    await self._ws.send_bytes(base64.b64decode(audio_data))

        elif event_type == ServerEventType.RESPONSE_AUDIO_DONE:
            logger.info("Agent audio complete")
            await self._send_json({"type": "status", "status": "ready"})

        elif event_type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_api_done = True

        # ── Errors ───────────────────────────────────────────────────
        elif event_type == ServerEventType.ERROR:
            error_msg = getattr(event.error, "message", str(event))
            if "no active response" in error_msg.lower():
                logger.debug("Benign cancellation error")
            else:
                logger.error("VoiceLive error: %s", error_msg)
                await self._send_json({"type": "error", "message": error_msg})

        # ── Informational ────────────────────────────────────────────
        elif event_type == ServerEventType.CONVERSATION_ITEM_CREATED:
            logger.debug(
                "Conversation item: %s", getattr(event.item, "id", "")
            )

        else:
            logger.debug("Unhandled event: %s", event_type)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_json(self, payload: dict) -> None:
        """Send a JSON text frame to the client WebSocket."""
        try:
            await self._ws.send_text(json.dumps(payload))
        except Exception:
            logger.debug("Failed to send JSON to client", exc_info=True)

    async def _send_error(self, message: str) -> None:
        """Send an error message to the client WebSocket."""
        await self._send_json({"type": "error", "message": message})
