from __future__ import annotations

import shutil

import pytest

from clipforge.engine.media.tools import ffmpeg_capabilities, parse_capabilities

VERSION = "ffmpeg version 7.1-full Copyright (c) 2000-2024\nconfiguration: --enable-libass --enable-nvenc\n"
ENCODERS = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder
"""
FILTERS = """Filters:
 ... ass               V->V       Render ASS subtitles
 ... subtitles         V->V       Render text subtitles
 ... crop              V->V       Crop
 ... scale             V->V       Scale
 ... loudnorm          A->A       EBU R128 loudness normalization
 ... ebur128           A->N       EBU R128 scanner
"""


def test_parse_capabilities_full() -> None:
    caps = parse_capabilities(VERSION, ENCODERS, FILTERS)
    assert caps.version == "7.1-full"
    assert caps.has_nvenc_h264 and not caps.has_nvenc_hevc
    assert caps.has_libx264 and caps.has_libass
    assert caps.missing_filters == ()
    assert caps.ok


def test_parse_capabilities_missing_libass() -> None:
    caps = parse_capabilities("ffmpeg version 6.0\n", ENCODERS, FILTERS.replace(" ass ", " xyz "))
    assert not caps.has_libass
    assert "ass" in caps.missing_filters
    assert not caps.ok


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_reports_version() -> None:
    caps = ffmpeg_capabilities("ffmpeg")
    assert caps.version != "unknown"
