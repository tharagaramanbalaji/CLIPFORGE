"""Storage interface. Keys are POSIX-style relative paths, e.g. `assets/<id>/proxy/<hash>/proxy.mp4`.

Engine code works on local paths. Workers fetch inputs to a local path, run the engine,
then `put_file` the outputs. S3 support (Phase 10) implements the same protocol.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from clipforge.errors import StorageError

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_key(key: str) -> str:
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") or not _SEGMENT_RE.match(p) for p in parts):
        raise StorageError(f"invalid storage key: {key!r}")
    return key


class Storage(Protocol):
    def put_file(self, src: Path, key: str, *, move: bool = False) -> None: ...
    def local_path(self, key: str) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def size(self, key: str) -> int: ...
    def delete(self, key: str) -> None: ...
    def move(self, src_key: str, dst_key: str) -> None: ...
