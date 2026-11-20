"""Application configuration, loaded from environment variables (prefix CLIPFORGE_) and .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Device(StrEnum):
    AUTO = "auto"
    CUDA = "cuda"
    CPU = "cpu"


class LLMBackend(StrEnum):
    CLAUDE_CODE = "claude_code"
    OLLAMA = "ollama"
    ANTHROPIC_API = "anthropic_api"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLIPFORGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Environment = Environment.DEV
    log_level: str = "INFO"
    log_json: bool = True

    # Infrastructure
    database_url: str = "postgresql+psycopg://clipforge:clipforge@localhost:5432/clipforge"
    redis_url: str = "redis://localhost:6379/0"

    # Storage
    data_dir: Path = Path("./data")
    tmp_dir: Path | None = None  # defaults to <data_dir>/tmp

    # Tools
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # Compute
    device: Device = Device.AUTO
    asr_model: str = "large-v3"
    asr_compute_type: str = "int8_float16"
    asr_batch_size: int = 16
    asr_vad_method: str = "pyannote"
    # CPU fallback (no GPU visible): large-v3 on CPU is ~1x realtime or slower, so default smaller.
    asr_cpu_model: str = "medium.en"
    asr_cpu_compute_type: str = "int8"
    asr_cpu_batch_size: int = 4
    # Phase 3 analysis
    diarize_model: str = "pyannote/speaker-diarization-community-1"
    audio_events_model: str = "MIT/ast-finetuned-audioset-10-10-0.4593"
    scene_threshold: float = 3.0

    # LLM (see ADR-0005: no paid API by default)
    llm_backend: LLMBackend = LLMBackend.CLAUDE_CODE
    anthropic_api_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    ollama_url: str = "http://host.docker.internal:11434"  # Ollama runs on the Windows host
    ollama_model: str = "qwen3:8b"
    # Host bridge to Claude Code (scripts/llm_bridge.py). Model aliases are Claude Code's (sonnet / opus).
    llm_bridge_url: str = "http://host.docker.internal:8765"
    llm_bridge_token: SecretStr | None = None
    llm_discover_model: str = "sonnet"
    llm_refine_model: str = "sonnet"
    highlight_window_min: int = 15
    highlight_refine_top_n: int = 10
    highlight_content_type: str = "talk"  # talk | gaming (per run: ?content_type=gaming)

    # Secrets
    secret_key: SecretStr = Field(default=SecretStr("dev-insecure-change-me"))
    hf_token: SecretStr | None = None

    # API / auth
    cors_origins: list[str] = ["http://localhost:5173"]
    session_cookie_name: str = "cf_session"
    session_ttl_hours: int = 24 * 7

    # Uploads & ingest limits
    upload_chunk_size: int = 16 * 1024 * 1024
    upload_session_ttl_hours: int = 48
    max_upload_bytes: int = 50 * 1024**3
    max_duration_s: int = 12 * 3600
    min_duration_s: float = 1.0
    # Video encoder for mezzanine/proxy: auto (NVENC when a GPU is visible) | nvenc | x264
    video_encoder: str = "auto"
    proxy_height: int = 540

    @property
    def uses_default_secret(self) -> bool:
        return self.secret_key.get_secret_value() in ("dev-insecure-change-me", "change-me")

    @property
    def resolved_tmp_dir(self) -> Path:
        return self.tmp_dir if self.tmp_dir is not None else self.data_dir / "tmp"


@lru_cache
def get_settings() -> Settings:
    return Settings()
