"""Long-running FFmpeg invocations with progress reporting, timeout and cancellation.

Short tool calls go through `run_command`; transcodes use this so a 3-hour encode can report progress
(`-progress pipe:1`) and be stopped when the pipeline run is canceled.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from clipforge.errors import Canceled, ExternalToolError, ExternalToolTimeout
from clipforge.log import get_logger

log = get_logger(__name__)

# Called with fraction done (0..1); return False to cancel.
ProgressFn = Callable[[float], bool]


def run_ffmpeg(
    ffmpeg_bin: str,
    args: Sequence[str | Path],
    *,
    duration_ms: int,
    timeout_s: float,
    on_progress: ProgressFn | None = None,
    env: dict[str, str] | None = None,
) -> None:
    argv = [ffmpeg_bin, "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1", *map(str, args)]
    deadline = time.monotonic() + timeout_s
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err, text=True, env=env)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if time.monotonic() > deadline:
                    proc.kill()
                    raise ExternalToolTimeout(f"ffmpeg timed out after {timeout_s:.0f}s")
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and on_progress is not None and duration_ms > 0:
                    try:
                        done = int(value) / 1000 / duration_ms
                    except ValueError:
                        continue
                    if not on_progress(min(max(done, 0.0), 1.0)):
                        proc.terminate()
                        raise Canceled("canceled")
            rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise ExternalToolTimeout(f"ffmpeg timed out after {timeout_s:.0f}s") from exc
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if rc != 0:
            err.seek(0)
            stderr = err.read().decode("utf-8", "replace")[-4000:]
            raise ExternalToolError(f"ffmpeg exited with code {rc}", details={"stderr": stderr})
    log.debug("ffmpeg_finished", args=argv[-1])
