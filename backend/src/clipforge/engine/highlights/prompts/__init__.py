"""Versioned prompt files. The version is stored on every llm_calls row; change it with the prompt."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DISCOVER_VERSION = "discover_v1"
REFINE_VERSION = "refine_v1"
JUDGE_VERSION = "judge_signals_v1"


@lru_cache
def load(version: str) -> str:
    return (Path(__file__).parent / f"{version}.md").read_text(encoding="utf-8")
