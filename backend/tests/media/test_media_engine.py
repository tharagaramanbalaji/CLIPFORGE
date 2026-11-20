"""Engine tests against real FFmpeg output. Run inside the container: pytest -m media"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.engine.media.probe import detect_vfr, probe_media
from clipforge.engine.media.transcode import (
    MezzanineConfig,
    ProxyConfig,
    make_mezzanine,
    make_proxy,
    needs_mezzanine,
)
from clipforge.engine.media.validate import IngestLimits, validate_probe
from clipforge.errors import ValidationFailed
from tests.fixtures_media import make_fixtures

pytestmark = pytest.mark.media


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return make_fixtures(tmp_path_factory.mktemp("media"))


@pytest.mark.parametrize(("name", "family"), [("mp4", "mp4"), ("mov", "mov"), ("mkv", "matroska")])
def test_probe_cfr_fixtures(media: dict[str, Path], name: str, family: str) -> None:
    p = probe_media(media[name])
    validate_probe(p, IngestLimits())
    assert family in p.format_name
    assert p.video is not None and (p.video.width, p.video.height) == (1280, 720)
    assert p.video.codec == "h264"
    assert p.audio is not None and p.audio.sample_rate == 48000
    assert abs(p.duration_ms - 4000) <= 100
    assert p.fps is not None and abs(float(p.fps) - 30) < 0.01
    assert detect_vfr(media[name], p).is_vfr is False


def test_vfr_fixture_detected_and_normalized(media: dict[str, Path], tmp_path: Path) -> None:
    p = probe_media(media["vfr"])
    report = detect_vfr(media["vfr"], p)
    assert report.is_vfr is True and report.method == "timestamps"
    assert needs_mezzanine(p, report.is_vfr)[0] is True

    out = tmp_path / "mez.mp4"
    progress: list[float] = []
    make_mezzanine(
        "ffmpeg",
        media["vfr"],
        out,
        p,
        MezzanineConfig(encoder="x264"),
        on_progress=lambda f: progress.append(f) or True,
    )
    mez = probe_media(out)
    assert detect_vfr(out, mez).is_vfr is False
    assert abs(mez.duration_ms - p.duration_ms) <= 150
    assert progress and progress[-1] > 0.5


def test_proxy_is_540p(media: dict[str, Path], tmp_path: Path) -> None:
    p = probe_media(media["mp4"])
    out = tmp_path / "proxy.mp4"
    make_proxy("ffmpeg", media["mp4"], out, p, ProxyConfig(encoder="x264"))
    proxy = probe_media(out)
    assert proxy.video is not None and proxy.video.height == 540 and proxy.video.width == 960
    assert proxy.audio is not None


def test_audio_only_rejected(media: dict[str, Path]) -> None:
    with pytest.raises(ValidationFailed, match="No video track"):
        validate_probe(probe_media(media["audio_only"]), IngestLimits())


@pytest.mark.parametrize("name", ["corrupt", "truncated"])
def test_unreadable_files_raise_clear_error(media: dict[str, Path], name: str) -> None:
    with pytest.raises(ValidationFailed, match="could not be read as a video"):
        probe_media(media[name])
