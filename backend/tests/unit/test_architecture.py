"""Guards the rule that the clipping engine is independent of FastAPI, Celery and the DB layer."""

from __future__ import annotations

import pkgutil
import subprocess
import sys

import clipforge.engine

FORBIDDEN = ("fastapi", "celery", "sqlalchemy", "clipforge.api", "clipforge.workers", "clipforge.db")


def test_engine_does_not_import_framework_layers() -> None:
    modules = [m.name for m in pkgutil.walk_packages(clipforge.engine.__path__, prefix="clipforge.engine.")]
    code = (
        "import importlib, sys\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        f"bad = sorted(n for n in sys.modules if n.split('.')[0] in {FORBIDDEN!r} "
        f"or any(n.startswith(p) for p in {FORBIDDEN!r}))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "", f"engine imported forbidden modules: {out.stdout}"
