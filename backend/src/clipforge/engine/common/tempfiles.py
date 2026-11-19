"""Per-task scratch directories that are always cleaned up."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def scratch_dir(base: Path, prefix: str = "job-") -> Iterator[Path]:
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
