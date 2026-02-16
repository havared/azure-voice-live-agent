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

    # ── Azure OpenAI Realtime ────────────────────────────────────────
    azure_openai_endpoint: str
    azure_openai_key: str
    azure_openai_deployment: str = "gpt-realtime"
    azure_api_version: str = "2025-10-01"

    # ── Voice ────────────────────────────────────────────────────────
    voice_name: str = "alloy"

    # ── VAD ─────────────────────────────────────────────────────────
    vad_threshold: float = 0.5
    vad_prefix_padding_ms: int = 300
    vad_silence_duration_ms: int = 500

    # ── Agent Instructions ───────────────────────────────────────────
    agent_instructions_file: str = "agent_instructions.md"

    # ── Application ─────────────────────────────────────────────────
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    enable_proactive_greeting: bool = True
