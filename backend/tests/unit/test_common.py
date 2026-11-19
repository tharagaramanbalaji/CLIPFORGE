from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clipforge.config import Device, Settings
from clipforge.engine.common.gpu import GpuInfo, parse_nvidia_smi_csv, resolve_device
from clipforge.engine.common.process import run_command
from clipforge.engine.common.tempfiles import scratch_dir
from clipforge.errors import ExternalToolError, ExternalToolTimeout

PY = sys.executable


def test_settings_read_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORGE_DEVICE", "cpu")
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", "/srv/data")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.device is Device.CPU
    assert s.resolved_tmp_dir == Path("/srv/data/tmp")
    assert s.anthropic_api_enabled is False  # ADR-0005: paid API off by default


def test_run_command_success() -> None:
    res = run_command([PY, "-c", "print('hi')"], timeout_s=30)
    assert res.returncode == 0
    assert res.stdout.strip() == "hi"


def test_run_command_failure_carries_stderr() -> None:
    with pytest.raises(ExternalToolError) as ei:
        run_command([PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], timeout_s=30)
    assert "code 3" in ei.value.message
    assert "boom" in str(ei.value.details["stderr"])
    assert ei.value.retryable is False


def test_run_command_timeout_is_retryable() -> None:
    with pytest.raises(ExternalToolTimeout) as ei:
        run_command([PY, "-c", "import time; time.sleep(5)"], timeout_s=0.5)
    assert ei.value.retryable is True


def test_run_command_missing_executable() -> None:
    with pytest.raises(ExternalToolError, match="not found"):
        run_command(["definitely-not-a-real-tool-xyz"], timeout_s=5)


def test_scratch_dir_cleaned_even_on_error(tmp_path: Path) -> None:
    created: Path | None = None
    with pytest.raises(RuntimeError), scratch_dir(tmp_path) as d:
        created = d
        (d / "f.bin").write_bytes(b"x")
        raise RuntimeError
    assert created is not None and not created.exists()


def test_parse_nvidia_smi_csv() -> None:
    gpus = parse_nvidia_smi_csv("NVIDIA GeForce RTX 3060, 12288, 610.88\ngarbage line\n")
    assert gpus == [GpuInfo("NVIDIA GeForce RTX 3060", 12288, "610.88")]


@pytest.mark.parametrize(
    ("setting", "gpus", "expected"),
    [
        (Device.AUTO, [GpuInfo("g", 1, "1")], Device.CUDA),
        (Device.AUTO, [], Device.CPU),
        (Device.CPU, [GpuInfo("g", 1, "1")], Device.CPU),
        (Device.CUDA, [], Device.CUDA),
    ],
)
def test_resolve_device(setting: Device, gpus: list[GpuInfo], expected: Device) -> None:
    assert resolve_device(setting, gpus) is expected
