"""File and config hashing used for integrity checks and idempotency keys."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

_BLOCK = 8 * 1024 * 1024


def sha256_file(path: Path, on_progress: Callable[[float], None] | None = None) -> str:
    total = path.stat().st_size or 1
    h = hashlib.sha256()
    done = 0
    with path.open("rb") as f:
        while block := f.read(_BLOCK):
            h.update(block)
            done += len(block)
            if on_progress is not None:
                on_progress(done / total)
    return h.hexdigest()


def stable_hash(*parts: Any) -> str:
    """sha256 of a canonical JSON encoding: same inputs -> same key, independent of dict order."""
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
