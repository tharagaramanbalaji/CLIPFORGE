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

    # LLM (see ADR-0005: no paid API by default)
    llm_backend: LLMBackend = LLMBackend.CLAUDE_CODE
    anthropic_api_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    ollama_url: str = "http://localhost:11434"

    # Secrets
    secret_key: SecretStr = Field(default=SecretStr("dev-insecure-change-me"))
    hf_token: SecretStr | None = None

    # API
    cors_origins: list[str] = ["http://localhost:5173"]

    @property
    def resolved_tmp_dir(self) -> Path:
        return self.tmp_dir if self.tmp_dir is not None else self.data_dir / "tmp"


@lru_cache
def get_settings() -> Settings:
    return Settings()
