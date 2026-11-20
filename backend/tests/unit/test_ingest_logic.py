"""Pure-logic tests for Phase 1: VFR classification, probe parsing, validation, transcode decisions,
upload chunk math, DAG ordering, hashing/IDs and session tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from clipforge.config import Settings
from clipforge.db.ids import uuid7
from clipforge.db.models import PipelineKind, StageName, UploadSession
from clipforge.engine.common.hashing import stable_hash
from clipforge.engine.media.probe import classify_frame_timing, parse_probe
from clipforge.engine.media.transcode import (
    MezzanineConfig,
    build_mezzanine_args,
    needs_mezzanine,
    target_fps,
)
from clipforge.engine.media.validate import IngestLimits, validation_errors
from clipforge.errors import Conflict, Gone, ValidationFailed
from clipforge.pipeline.dag import StageDef, dag_for, validate_dag
from clipforge.services.auth import issue_token, read_token
from clipforge.services.uploads import check_chunk, clean_filename


def _probe_json(**over: Any) -> dict[str, Any]:
    video = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "pix_fmt": "yuv420p",
        "disposition": {},
    }
    audio = {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000"}
    data: dict[str, Any] = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "600.5",
            "size": "1000",
            "bit_rate": "8000000",
        },
        "streams": [video, audio],
    }
    for k, v in over.items():
        if k == "video":
            data["streams"][0].update(v)
        elif k == "format":
            data["format"].update(v)
        elif k == "streams":
            data["streams"] = v
    return data


# --- VFR -------------------------------------------------------------------------------------------


def test_cfr_timestamps_are_not_vfr() -> None:
    pts = [i * 1000 / 30 for i in range(300)]
    assert classify_frame_timing(pts).is_vfr is False


def test_millisecond_timebase_jitter_is_not_vfr() -> None:
    # MKV/FLV store ms timestamps: 29.97 fps shows up as 33/34 ms alternation
    pts = [float(round(i * 1001 / 30)) for i in range(300)]
    report = classify_frame_timing(pts)
    assert report.is_vfr is False
    assert report.method == "timestamps"


def test_irregular_intervals_are_vfr() -> None:
    pts = [(i + i // 3) * 1000 / 30 for i in range(300)]  # every third interval doubles
    report = classify_frame_timing(pts)
    assert report.is_vfr is True
    assert report.irregular_ratio is not None and report.irregular_ratio > 0.2


def test_too_few_frames_falls_back_to_metadata() -> None:
    assert classify_frame_timing([0, 33, 66], rates_differ=True).method == "metadata"


# --- probe parsing & validation --------------------------------------------------------------------


def test_parse_probe_fields() -> None:
    p = parse_probe(_probe_json())
    assert p.duration_ms == 600_500
    assert p.video is not None and p.video.width == 1920
    assert p.audio is not None and p.audio.sample_rate == 48000
    assert p.fps == Fraction(30)


def test_cover_art_is_not_the_main_video_stream() -> None:
    cover = {
        "index": 2,
        "codec_type": "video",
        "codec_name": "mjpeg",
        "width": 600,
        "height": 600,
        "r_frame_rate": "90000/1",
        "avg_frame_rate": "0/0",
        "disposition": {"attached_pic": 1},
    }
    data = _probe_json()
    data["streams"].insert(0, cover)
    p = parse_probe(data)
    assert p.video is not None and p.video.codec == "h264"


def test_valid_probe_has_no_errors() -> None:
    assert validation_errors(parse_probe(_probe_json()), IngestLimits()) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        (
            {
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "channels": 2,
                        "sample_rate": "48000",
                    }
                ]
            },
            "No video track",
        ),
        ({"format": {"format_name": "wav"}}, "Unsupported file type"),
        ({"video": {"codec_name": "rawvideo"}}, "Unsupported video codec"),
        ({"format": {"duration": "50000"}}, "limit is 12 h"),
        ({"format": {"duration": "0.2"}}, "too short"),
    ],
)
def test_validation_rejections(over: dict[str, Any], needle: str) -> None:
    errors = validation_errors(parse_probe(_probe_json(**over)), IngestLimits())
    assert any(needle in e for e in errors), errors


def test_missing_audio_is_rejected() -> None:
    data = _probe_json()
    data["streams"] = data["streams"][:1]
    assert any("No audio track" in e for e in validation_errors(parse_probe(data), IngestLimits()))


# --- transcode decisions ---------------------------------------------------------------------------


def test_cfr_h264_mp4_is_passthrough() -> None:
    assert needs_mezzanine(parse_probe(_probe_json()), is_vfr=False)[0] is False


@pytest.mark.parametrize(
    ("over", "vfr"),
    [
        ({}, True),
        ({"video": {"codec_name": "vp9"}}, False),
        ({"format": {"format_name": "flv"}}, False),
        ({"video": {"pix_fmt": "yuv420p10le"}}, False),
    ],
)
def test_mezzanine_needed(over: dict[str, Any], vfr: bool) -> None:
    assert needs_mezzanine(parse_probe(_probe_json(**over)), is_vfr=vfr)[0] is True


@pytest.mark.parametrize(
    ("avg", "expected"),
    [
        ("2997/100", Fraction(30000, 1001)),
        ("5994/100", Fraction(60000, 1001)),
        ("144/1", Fraction(60)),
        ("23/1", Fraction(23)),
    ],
)
def test_target_fps_snaps_and_caps(avg: str, expected: Fraction) -> None:
    p = parse_probe(_probe_json(video={"avg_frame_rate": avg, "r_frame_rate": avg}))
    assert target_fps(p, 60) == expected


def test_mezzanine_args_force_cfr() -> None:
    args = build_mezzanine_args(
        Path("in.mkv"), Path("out.mp4"), parse_probe(_probe_json()), MezzanineConfig(encoder="x264")
    )
    assert args[args.index("-fps_mode") + 1] == "cfr"
    assert "libx264" in args and "-g" in args


# --- uploads ---------------------------------------------------------------------------------------


def _session(total: int, chunk: int, **kw: Any) -> UploadSession:
    return UploadSession(
        total_bytes=total,
        chunk_size=chunk,
        parts=[],
        storage_key="k",
        expires_at=kw.get("expires_at", datetime.now(UTC) + timedelta(hours=1)),
        completed_at=kw.get("completed_at"),
    )


def test_chunk_math() -> None:
    up = _session(total=25, chunk=10)
    assert up.total_chunks == 3
    assert [up.expected_chunk_len(i) for i in range(3)] == [10, 10, 5]


def test_check_chunk_rules() -> None:
    now = datetime.now(UTC)
    up = _session(total=25, chunk=10)
    check_chunk(up, 2, 5, now)
    with pytest.raises(ValidationFailed):
        check_chunk(up, 2, 10, now)
    with pytest.raises(ValidationFailed):
        check_chunk(up, 3, 10, now)
    with pytest.raises(Gone):
        check_chunk(_session(25, 10, expires_at=now - timedelta(seconds=1)), 0, 10, now)
    with pytest.raises(Conflict):
        check_chunk(_session(25, 10, completed_at=now), 0, 10, now)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("stream.mp4", "stream.mp4"),
        ("C:\\Users\\x\\My VOD (1).mkv", "My VOD _1_.mkv"),
        ("../../etc/passwd", "passwd"),
        ("...", "upload"),
    ],
)
def test_clean_filename(raw: str, clean: str) -> None:
    assert clean_filename(raw) == clean


# --- pipeline DAG ----------------------------------------------------------------------------------


def test_analysis_dag_order() -> None:
    assert [s.name for s in dag_for(PipelineKind.ANALYSIS)] == [
        StageName.PROBE,
        StageName.MEZZANINE,
        StageName.PROXY,
        StageName.AUDIO_EXTRACT,
        StageName.TRANSCRIBE,
        StageName.DIARIZE,
        StageName.SCENES,
        StageName.SIGNALS,
    ]


def test_validate_dag_rejects_forward_dependency() -> None:
    with pytest.raises(ValueError):
        validate_dag(
            (StageDef(StageName.PROXY, "cpu", deps=(StageName.PROBE,)), StageDef(StageName.PROBE, "cpu"))
        )


# --- hashing, ids, tokens --------------------------------------------------------------------------


def test_stable_hash_ignores_dict_order() -> None:
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})


def test_uuid7_is_versioned_and_time_ordered() -> None:
    ids = [uuid7() for _ in range(50)]
    assert all(i.version == 7 for i in ids)
    assert [i.int >> 80 for i in ids] == sorted(i.int >> 80 for i in ids)


def test_session_token_roundtrip_and_tamper() -> None:
    settings = Settings(secret_key="unit-test-secret-0123456789abcdef")  # type: ignore[arg-type]
    uid = uuid.uuid4()
    token = issue_token(uid, settings)
    assert read_token(token, settings) == uid
    assert read_token(token[:-2] + "xx", settings) is None
    other = Settings(secret_key="another-secret-0123456789abcdef!!")  # type: ignore[arg-type]
    assert read_token(token, other) is None


def test_token_tolerates_small_clock_steps() -> None:
    """Regression: the Docker VM clock steps back slightly on resync; a token 'issued' 30 s ahead must work."""
    import jwt

    settings = Settings(secret_key="unit-test-secret-0123456789abcdef")  # type: ignore[arg-type]
    uid = uuid.uuid4()
    now = datetime.now(UTC)
    ahead = jwt.encode(
        {"sub": str(uid), "iat": now + timedelta(seconds=30), "exp": now + timedelta(hours=1)},
        "unit-test-secret-0123456789abcdef",
        algorithm="HS256",
    )
    assert read_token(ahead, settings) == uid
    expired = jwt.encode(
        {"sub": str(uid), "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        "unit-test-secret-0123456789abcdef",
        algorithm="HS256",
    )
    assert read_token(expired, settings) is None
