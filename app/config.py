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

    # ── Azure Voice Live ────────────────────────────────────────────
    azure_voicelive_endpoint: str
    azure_voicelive_project_name: str
    azure_voicelive_agent_id: str
    azure_voicelive_api_version: str = "2025-10-01"
    azure_voicelive_api_key: str

    # ── Service Principal (for agent access token, no az login) ─────
    azure_tenant_id: str
    azure_client_id: str
    azure_client_secret: str

    # ── Custom Voice ────────────────────────────────────────────────
    azure_voicelive_voice_name: str
    azure_voicelive_voice_endpoint_id: str

    # ── VAD ─────────────────────────────────────────────────────────
    vad_threshold: float = 0.5
    vad_prefix_padding_ms: int = 300
    vad_silence_duration_ms: int = 500

    # ── Application ─────────────────────────────────────────────────
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    enable_proactive_greeting: bool = True
