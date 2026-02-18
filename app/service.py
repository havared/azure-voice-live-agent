"""Voice Live session manager.

Bridges a client WebSocket connection to Azure OpenAI Realtime via Voice Live
for real-time speech-to-speech voice interactions.

WebSocket Protocol
==================

Client -> Server
    Binary frame : Raw PCM16 24 kHz mono audio bytes
    Text frame   : JSON  {"type": "audio", "audio": "<base64 PCM16>"}
                         {"type": "barge_in"}

Server -> Client
    Binary frame : Raw PCM16 24 kHz mono audio bytes  (agent speech)
    Text frame   : JSON control messages
        {"type": "session_started", "session_id": "..."}
        {"type": "user_transcript", "text": "..."}
        {"type": "agent_transcript", "text": "..."}
        {"type": "agent_text", "text": "..."}
        {"type": "clear_playback"}
        {"type": "status", "status": "listening | processing | agent_speaking | ready"}
        {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from azure.ai.voicelive.aio import VoiceLiveConnection, connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureSemanticDetectionEn,
    AzureSemanticVadEn,
    AzureStandardVoice,
    EouThresholdLevel,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
)
from azure.core.credentials import AzureKeyCredential
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
        self._audio_suppressed: bool = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Open a Voice Live session and relay audio until disconnect."""
        try:
            credential = AzureKeyCredential(self._settings.azure_openai_key)

            query_params = urlencode({
                "api-version": self._settings.azure_api_version,
                "model": self._settings.azure_model,
            })
            ws_url = (
                f"{self._settings.azure_openai_endpoint.rstrip('/')}"
                f"/voice-live/realtime?{query_params}"
            )
            logger.info("Connecting to Voice Live | url=%s", ws_url)

            async with connect(
                endpoint=self._settings.azure_openai_endpoint,
                credential=credential,
                model=self._settings.azure_model,
                api_version=self._settings.azure_api_version,
            ) as connection:
                self._connection = connection
                logger.info("Voice Live connection established")

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
    # Session configuration
    # ------------------------------------------------------------------

    def _load_instructions(self) -> str:
        """Load agent instructions from the configured markdown file."""
        file_path = Path(self._settings.agent_instructions_file)
        if not file_path.is_file():
            logger.warning(
                "Instructions file not found: %s — using empty instructions",
                file_path,
            )
            return ""

        text = file_path.read_text(encoding="utf-8").strip()
        logger.info(
            "Loaded agent instructions from %s (%d chars)", file_path, len(text)
        )
        return text

    async def _configure_session(self) -> None:
        """Send ``session.update`` with Azure TTS voice, semantic VAD, echo-cancel & noise-reduce."""
        assert self._connection is not None

        instructions = self._load_instructions()

        eou_detection = None
        if self._settings.eou_enabled:
            eou_detection = AzureSemanticDetectionEn(
                threshold_level=EouThresholdLevel(self._settings.eou_threshold_level),
                timeout_ms=self._settings.eou_timeout_ms,
            )

        turn_detection = AzureSemanticVadEn(
            threshold=self._settings.vad_threshold,
            prefix_padding_ms=self._settings.vad_prefix_padding_ms,
            silence_duration_ms=self._settings.vad_silence_duration_ms,
            remove_filler_words=self._settings.remove_filler_words,
            end_of_utterance_detection=eou_detection,
        )

        voice = AzureStandardVoice(
            name=self._settings.voice_name,
            temperature=self._settings.voice_temperature,
        )

        input_transcription = AudioInputTranscriptionOptions(
            model=self._settings.input_audio_transcription_model,
            language=self._settings.input_audio_transcription_language,
        )

        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=instructions,
            voice=voice,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=turn_detection,
            input_audio_transcription=input_transcription,
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
        )

        await self._connection.session.update(session=session_config)
        logger.info("Session configuration sent (voice=%s)", self._settings.voice_name)

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
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

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
                elif msg_type == "barge_in":
                    await self._handle_client_barge_in()
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
            transcript = getattr(event, "transcript", "") or event.get("transcript", "")
            logger.info("User: %s", transcript)
            if transcript:
                await self._send_json({"type": "user_transcript", "text": transcript})

        elif (
            event_type
            == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED
        ):
            error_msg = getattr(event.error, "message", str(event)) if hasattr(event, "error") else str(event)
            logger.warning("User transcription failed: %s", error_msg)

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
                self._audio_suppressed = True
                await self._send_json({"type": "clear_playback"})
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
            self._audio_suppressed = False
            await self._send_json({"type": "status", "status": "agent_speaking"})

        elif event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
            if self._audio_suppressed:
                return
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
    # Barge-in
    # ------------------------------------------------------------------

    async def _handle_client_barge_in(self) -> None:
        """Handle a barge-in signal sent by the client's local VAD.

        Immediately suppresses audio forwarding and cancels the active
        response so the agent stops speaking without waiting for Azure's
        server-side VAD round-trip.
        """
        assert self._connection is not None
        self._audio_suppressed = True
        logger.info("Client barge-in received — audio suppressed")

        if self._active_response and not self._response_api_done:
            try:
                await self._connection.response.cancel()
                logger.info("Cancelled active response (client barge-in)")
            except Exception as exc:
                msg = str(exc).lower()
                if "no active response" in msg:
                    logger.debug("Cancel ignored — already completed")
                else:
                    logger.warning("Cancel failed: %s", exc)

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
