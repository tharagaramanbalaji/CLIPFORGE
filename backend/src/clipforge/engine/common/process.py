"""Single entry point for running external tools (ffmpeg, ffprobe, nvidia-smi, ...).

Always argument lists (never shell strings), always a timeout, stderr captured
for error reports, structured log line per call.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clipforge.errors import ExternalToolError, ExternalToolTimeout
from clipforge.log import get_logger

log = get_logger(__name__)

_STDERR_TAIL = 4000


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


def run_command(
    args: Sequence[str | Path],
    *,
    timeout_s: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    argv = tuple(str(a) for a in args)
    if not argv:
        raise ValueError("args must not be empty")
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ExternalToolError(f"executable not found: {argv[0]}", details={"args": argv}) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExternalToolTimeout(f"{argv[0]} timed out after {timeout_s}s", details={"args": argv}) from exc

    result = CommandResult(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start)
    log.debug("command_finished", tool=Path(argv[0]).name, rc=result.returncode, duration_s=result.duration_s)
    if check and result.returncode != 0:
        raise ExternalToolError(
            f"{Path(argv[0]).name} exited with code {result.returncode}",
            details={"args": argv, "stderr": result.stderr[-_STDERR_TAIL:]},
        )
    return result
