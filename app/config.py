"""Application configuration loaded from environment variables.

Uses pydantic-settings for type-safe validation. All values are sourced
from the .env file or actual environment variables.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for the Voice Live API server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Azure Voice Live ─────────────────────────────────────────────
    azure_openai_endpoint: str
    azure_openai_key: str
    azure_model: str = "gpt-realtime"
    azure_api_version: str = "2025-10-01"

    # ── Voice (Azure TTS) ────────────────────────────────────────────
    voice_name: str = "en-US-AvaNeural"
    voice_temperature: float = 0.8

    # ── Azure Semantic VAD ───────────────────────────────────────────
    vad_threshold: float = 0.3
    vad_prefix_padding_ms: int = 200
    vad_silence_duration_ms: int = 200
    remove_filler_words: bool = False

    # ── End-of-Utterance Detection ───────────────────────────────────
    # Only supported with cascaded (non-realtime) models like gpt-4o.
    # Not supported with gpt-realtime, gpt-4o-mini-realtime, phi4-mm-realtime.
    eou_enabled: bool = False
    eou_threshold_level: str = "default"
    eou_timeout_ms: int = 1000

    # ── Input Audio Transcription ───────────────────────────────────
    # Model used to transcribe user speech. Supported values:
    # "whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "azure-speech"
    input_audio_transcription_model: str = "azure-speech"
    input_audio_transcription_language: str = "en"

    # ── Agent Instructions ───────────────────────────────────────────
    agent_instructions_file: str = "agent_instructions.md"

    # ── Application ─────────────────────────────────────────────────
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    enable_proactive_greeting: bool = True
    admin_password: str = "admin"
