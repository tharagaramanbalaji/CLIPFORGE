"""Local filesystem storage with atomic writes."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from clipforge.errors import StorageError
from clipforge.storage.base import validate_key


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError(f"key escapes storage root: {key!r}")
        return path

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def put_file(self, src: Path, key: str, *, move: bool = False) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
        try:
            if move:
                shutil.move(src, tmp)
            else:
                shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"failed to store {key!r}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def size(self, key: str) -> int:
        try:
            return self._path(key).stat().st_size
        except FileNotFoundError as exc:
            raise StorageError(f"not found: {key!r}") from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
