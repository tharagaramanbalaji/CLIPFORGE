"""Phase 1 acceptance tests through the HTTP API against real Postgres and real FFmpeg."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clipforge.api.deps import dispatch_dep
from clipforge.api.main import create_app
from clipforge.services import events
from tests.fixtures_media import make_fixtures
from tests.integration.conftest import InlineDispatcher

pytestmark = [pytest.mark.integration, pytest.mark.media]

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return make_fixtures(tmp_path_factory.mktemp("media"))


@pytest.fixture
def env(clean_db: None) -> Iterator[tuple[TestClient, InlineDispatcher, events.MemoryPublisher]]:
    dispatcher = InlineDispatcher()
    publisher = events.MemoryPublisher()
    events.set_publisher(publisher)
    app = create_app()
    app.dependency_overrides[dispatch_dep] = lambda: dispatcher
    with TestClient(app) as client:
        yield client, dispatcher, publisher
    events.set_publisher(None)


def _ok(r: Any, code: int = 200) -> Any:
    assert r.status_code == code, f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text}"
    return r.json()


def _owner(client: TestClient) -> None:
    _ok(
        client.post(
            "/api/v1/auth/register",
            json={"email": "owner@example.com", "password": PASSWORD, "display_name": "Owner"},
        ),
        201,
    )


def _setup(client: TestClient) -> dict[str, str]:
    _owner(client)
    creator = _ok(client.post("/api/v1/creators", json={"name": "Streamer A"}), 201)
    grant = _ok(
        client.post(
            f"/api/v1/creators/{creator['id']}/rights-grants",
            json={"basis": "written_permission", "evidence_note": "Email 2026-11-01"},
        ),
        201,
    )
    project = _ok(
        client.post("/api/v1/projects", json={"name": "VOD clips", "creator_id": creator["id"]}), 201
    )
    return {"creator": creator["id"], "grant": grant["id"], "project": project["id"]}


def _start(client: TestClient, ids: dict[str, str], path: Path, name: str | None = None) -> dict[str, Any]:
    return _ok(
        client.post(
            f"/api/v1/projects/{ids['project']}/uploads",
            json={
                "filename": name or path.name,
                "size_bytes": path.stat().st_size,
                "rights_grant_id": ids["grant"],
            },
        ),
        201,
    )


def _put(client: TestClient, up: dict[str, Any], data: bytes, n: int, *, checksum: bool = True) -> Any:
    chunk = data[n * up["chunk_size"] : (n + 1) * up["chunk_size"]]
    headers = {"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()} if checksum else {}
    return client.put(f"/api/v1/uploads/{up['upload_id']}/chunks/{n}", content=chunk, headers=headers)


def _upload_all(
    client: TestClient, ids: dict[str, str], path: Path, name: str | None = None
) -> dict[str, Any]:
    up = _start(client, ids, path, name)
    data = path.read_bytes()
    for n in range(up["total_chunks"]):
        _ok(_put(client, up, data, n))
    return _ok(client.post(f"/api/v1/uploads/{up['upload_id']}/complete"))


# --- auth --------------------------------------------------------------------------------------------


def test_single_owner_registration_and_login(env: Any) -> None:
    client, _, _ = env
    assert client.get("/api/v1/creators").status_code == 401
    _owner(client)
    assert _ok(client.get("/api/v1/auth/me"))["role"] == "owner"
    r = client.post(
        "/api/v1/auth/register", json={"email": "x@example.com", "password": PASSWORD, "display_name": "X"}
    )
    assert r.status_code == 403 and "closed" in r.json()["error"]["message"]
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "owner@example.com", "password": "nope-nope-nope"}
        ).status_code
        == 401
    )
    _ok(client.post("/api/v1/auth/login", json={"email": "OWNER@example.com", "password": PASSWORD}))
    assert client.get("/api/v1/auth/me").status_code == 200


def test_event_stream_does_not_hold_a_db_transaction(env: Any) -> None:
    import threading

    from sqlalchemy import text

    from clipforge.db.session import get_engine

    client, _, _ = env
    _owner(client)
    stop = threading.Event()

    def listen() -> None:
        with client.stream("GET", "/api/v1/events/stream") as r:
            assert r.status_code == 200
            for _ in r.iter_lines():
                if stop.is_set():
                    break

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    import time

    time.sleep(1.5)
    with get_engine().connect() as conn:
        idle = conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction'"
            )
        ).scalar()
    stop.set()
    assert idle == 0


# --- rights gate -------------------------------------------------------------------------------------


def test_upload_without_valid_rights_grant_is_rejected(env: Any, media: dict[str, Path]) -> None:
    client, _, _ = env
    ids = _setup(client)
    size = media["mp4"].stat().st_size
    url = f"/api/v1/projects/{ids['project']}/uploads"

    r = client.post(url, json={"filename": "a.mp4", "size_bytes": size})
    assert r.status_code == 422  # rights_grant_id is required

    other = _ok(client.post("/api/v1/creators", json={"name": "Someone else"}), 201)
    other_grant = _ok(
        client.post(f"/api/v1/creators/{other['id']}/rights-grants", json={"basis": "own_content"}), 201
    )
    r = client.post(url, json={"filename": "a.mp4", "size_bytes": size, "rights_grant_id": other_grant["id"]})
    assert r.status_code == 403 and "different creator" in r.json()["error"]["message"]

    expired = _ok(
        client.post(
            f"/api/v1/creators/{ids['creator']}/rights-grants",
            json={
                "basis": "license_agreement",
                "valid_from": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
                "valid_until": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            },
        ),
        201,
    )
    assert expired["is_active"] is False
    r = client.post(url, json={"filename": "a.mp4", "size_bytes": size, "rights_grant_id": expired["id"]})
    assert r.status_code == 403

    _ok(client.post(f"/api/v1/rights-grants/{ids['grant']}/revoke"))
    r = client.post(url, json={"filename": "a.mp4", "size_bytes": size, "rights_grant_id": ids["grant"]})
    assert r.status_code == 403 and "revoked" in r.json()["error"]["message"]


def test_grant_revoked_mid_upload_blocks_completion(env: Any, media: dict[str, Path]) -> None:
    client, _, _ = env
    ids = _setup(client)
    up = _start(client, ids, media["mp4"])
    data = media["mp4"].read_bytes()
    for n in range(up["total_chunks"]):
        _ok(_put(client, up, data, n))
    _ok(client.post(f"/api/v1/rights-grants/{ids['grant']}/revoke"))
    assert client.post(f"/api/v1/uploads/{up['upload_id']}/complete").status_code == 403


# --- resumable upload --------------------------------------------------------------------------------


def test_interrupted_upload_resumes_and_ingests(env: Any, media: dict[str, Path], data_dir: Path) -> None:
    client, dispatcher, publisher = env
    ids = _setup(client)
    path = media["mp4"]
    data = path.read_bytes()
    up = _start(client, ids, path)
    assert up["total_chunks"] >= 3, "fixture must span several chunks"

    # First attempt: the connection "drops" after half the chunks.
    half = up["total_chunks"] // 2
    for n in range(half):
        _ok(_put(client, up, data, n))
    r = client.post(f"/api/v1/uploads/{up['upload_id']}/complete")
    assert r.status_code == 409 and r.json()["error"]["details"]["missing"][0] == half

    # Bad requests are rejected without corrupting state.
    bad = client.put(f"/api/v1/uploads/{up['upload_id']}/chunks/{half}", content=b"short")
    assert bad.status_code == 422
    chunk = data[half * up["chunk_size"] : (half + 1) * up["chunk_size"]]
    bad = client.put(
        f"/api/v1/uploads/{up['upload_id']}/chunks/{half}",
        content=chunk,
        headers={"X-Chunk-SHA256": "0" * 64},
    )
    assert bad.status_code == 422 and "checksum" in bad.json()["error"]["message"]

    # Resume: ask the server what it has, send only the rest (one chunk twice to prove idempotency).
    status = _ok(client.get(f"/api/v1/uploads/{up['upload_id']}"))
    assert status["received_chunks"] == list(range(half))
    for n in [i for i in range(up["total_chunks"]) if i not in status["received_chunks"]] + [half]:
        _ok(_put(client, up, data, n))
    status = _ok(client.get(f"/api/v1/uploads/{up['upload_id']}"))
    assert status["received_bytes"] == len(data)

    done = _ok(client.post(f"/api/v1/uploads/{up['upload_id']}/complete"))
    assert done["status"] == "validating"
    assert [q for _, q in dispatcher.queued] == ["cpu"]
    dispatcher.drain()
    executed = [s for s, _ in dispatcher.executed]
    assert executed[:2] == ["probe", "mezzanine"]
    assert sorted(executed) == sorted(
        ["probe", "mezzanine", "proxy", "audio_extract", "transcribe", "diarize", "scenes", "signals"]
    )
    assert executed.index("transcribe") < min(executed.index("diarize"), executed.index("signals"))
    queues = dict(dispatcher.executed)
    assert [queues[s] for s in ("transcribe", "diarize", "signals", "scenes")] == ["gpu", "gpu", "gpu", "cpu"]

    m = _ok(client.get(f"/api/v1/media/{done['media_id']}"))
    assert m["status"] == "ready", m
    assert m["sha256"] == hashlib.sha256(data).hexdigest()  # reassembled bytes are identical
    assert (m["width"], m["height"], m["video_codec"], m["is_vfr"]) == (1280, 720, "h264", False)
    assert m["fps_num"] / m["fps_den"] == 30 and abs(m["duration_ms"] - 4000) <= 100
    assert m["audio_sample_rate"] == 48000 and m["audio_channels"] == 1
    stages = {s["stage"]: s for s in m["latest_run"]["stages"]}
    assert stages["mezzanine"]["metrics"]["transcoded"] is False  # CFR H.264 => original is the mezzanine
    assert any(e["type"] == "run" and e["status"] == "succeeded" for _, e in publisher.events)
    assert list(data_dir.glob(f"assets/{m['id']}/proxy/*/proxy.mp4"))
    assert not list(data_dir.glob(f"uploads/{up['upload_id']}/*"))

    # Completing again is idempotent.
    again = _ok(client.post(f"/api/v1/uploads/{up['upload_id']}/complete"))
    assert again["pipeline_run_id"] == done["pipeline_run_id"]


@pytest.mark.parametrize(
    ("name", "container", "vfr"), [("mov", "mov", False), ("mkv", "matroska", False), ("vfr", "mp4", True)]
)
def test_metadata_for_containers_and_vfr(
    env: Any, media: dict[str, Path], name: str, container: str, vfr: bool
) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media[name])
    dispatcher.drain()
    m = _ok(client.get(f"/api/v1/media/{done['media_id']}"))
    assert m["status"] == "ready", m
    assert container in m["container"] and m["is_vfr"] is vfr
    assert (m["width"], m["height"]) == (1280, 720)
    stages = {s["stage"]: s for s in m["latest_run"]["stages"]}
    assert stages["probe"]["metrics"]["is_vfr"] is vfr
    if vfr:
        assert stages["mezzanine"]["metrics"]["transcoded"] is True
        assert stages["mezzanine"]["metrics"]["reason"] == "variable frame rate"


@pytest.mark.parametrize(
    ("name", "upload_as", "message"),
    [
        ("corrupt", None, "could not be read as a video"),
        ("truncated", None, "could not be read as a video"),
        ("audio_only", None, "No video track"),
    ],
)
def test_bad_files_fail_with_clear_errors(
    env: Any, media: dict[str, Path], name: str, upload_as: str | None, message: str, data_dir: Path
) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media[name], upload_as)
    dispatcher.drain()
    m = _ok(client.get(f"/api/v1/media/{done['media_id']}"))
    assert m["status"] == "failed"
    assert message in m["error"]["message"] and m["error"]["stage"] == "probe"
    assert str(data_dir) not in str(m["error"]) and "args" not in m["error"]["details"]
    stages = {s["stage"]: s["status"] for s in m["latest_run"]["stages"]}
    assert stages == {
        "probe": "failed",
        "mezzanine": "canceled",
        "proxy": "canceled",
        "audio_extract": "canceled",
        "transcribe": "canceled",
        "diarize": "canceled",
        "scenes": "canceled",
        "signals": "canceled",
    }


def test_unsupported_extension_rejected_before_upload(env: Any, media: dict[str, Path]) -> None:
    client, _, _ = env
    ids = _setup(client)
    r = client.post(
        f"/api/v1/projects/{ids['project']}/uploads",
        json={"filename": "notes.txt", "size_bytes": 10, "rights_grant_id": ids["grant"]},
    )
    assert r.status_code == 422 and "Unsupported file type" in r.json()["error"]["message"]


def test_rerunning_analysis_is_a_noop(env: Any, media: dict[str, Path]) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["vfr"])
    dispatcher.drain()
    before = _ok(client.get(f"/api/v1/media/{done['media_id']}"))

    executed_before = len(dispatcher.executed)
    run = _ok(client.post(f"/api/v1/media/{done['media_id']}/analyze"))
    assert run["status"] == "succeeded"
    assert {s["status"] for s in run["stages"]} == {"skipped"}
    assert all("reused_stage_run_id" in s["metrics"] for s in run["stages"])
    assert not dispatcher.queued and len(dispatcher.executed) == executed_before

    after = _ok(client.get(f"/api/v1/media/{done['media_id']}"))
    for key in ("sha256", "duration_ms", "width", "height", "fps_num", "fps_den", "is_vfr", "status"):
        assert after[key] == before[key]


def test_cancel_and_retry(env: Any, media: dict[str, Path]) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    canceled = _ok(client.post(f"/api/v1/pipeline-runs/{done['pipeline_run_id']}/cancel"))
    assert canceled["status"] == "canceled"
    dispatcher.drain()  # the queued probe task sees the cancel and stops
    retried = _ok(client.post(f"/api/v1/pipeline-runs/{done['pipeline_run_id']}/retry"))
    assert retried["status"] == "running"
    dispatcher.drain()
    assert _ok(client.get(f"/api/v1/media/{done['media_id']}"))["status"] == "ready"


# --- Phase 2: audio + transcript -----------------------------------------------------------------------


def test_transcript_persisted_and_range_query(
    env: Any, media: dict[str, Path], fake_transcriber: Any
) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    m = _ok(client.get(f"/api/v1/media/{done['media_id']}"))
    assert m["status"] == "ready"
    stages = {s["stage"]: s for s in m["latest_run"]["stages"]}
    assert stages["transcribe"]["metrics"]["model"] and "device" in stages["transcribe"]["metrics"]

    full = _ok(client.get(f"/api/v1/media/{done['media_id']}/transcript"))
    assert full["segment_count"] == 4 and len(full["segments"]) == 4  # 4 s fixture
    assert [s["idx"] for s in full["segments"]] == [0, 1, 2, 3]
    assert full["segments"][1]["words"][1] == {"w": "number1", "s": 1250, "e": 1600, "p": 0.8, "spk": "S2"}

    part = _ok(
        client.get(
            f"/api/v1/media/{done['media_id']}/transcript",
            params={"from_ms": 1500, "to_ms": 2100, "include_words": "false"},
        )
    )
    assert [s["idx"] for s in part["segments"]] == [1, 2]  # overlap semantics: [1000,1900) and [2000,2900)
    assert part["segments"][0]["words"] is None
    assert (
        client.get(
            f"/api/v1/media/{done['media_id']}/transcript", params={"from_ms": 500, "to_ms": 100}
        ).status_code
        == 422
    )

    # Re-analysis reuses the transcript instead of transcribing again
    calls = fake_transcriber.calls
    run = _ok(client.post(f"/api/v1/media/{done['media_id']}/analyze"))
    assert {s["status"] for s in run["stages"]} == {"skipped"} and fake_transcriber.calls == calls


def test_transcript_404_before_transcription(env: Any, media: dict[str, Path]) -> None:
    client, _, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])  # not drained: no transcript yet
    r = client.get(f"/api/v1/media/{done['media_id']}/transcript")
    assert r.status_code == 404


def test_changing_asr_model_reruns_only_transcription(
    env: Any, media: dict[str, Path], fake_transcriber: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.config import get_settings

    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    monkeypatch.setattr(get_settings(), "asr_model", "large-v3-turbo")
    monkeypatch.setattr(get_settings(), "asr_cpu_model", "small.en")
    run = _ok(client.post(f"/api/v1/media/{done['media_id']}/analyze"))
    dispatcher.drain()
    run = _ok(client.get(f"/api/v1/pipeline-runs/{run['id']}"))
    status = {s["stage"]: s["status"] for s in run["stages"]}
    assert status == {
        "probe": "skipped",
        "mezzanine": "skipped",
        "proxy": "skipped",
        "audio_extract": "skipped",
        "transcribe": "succeeded",
        "diarize": "succeeded",  # depends on the transcript, so it re-runs too
        "scenes": "skipped",
        "signals": "succeeded",
    }
    t = _ok(client.get(f"/api/v1/media/{done['media_id']}/transcript"))
    assert t["segment_count"] == 4  # new transcript is current; old one kept but not current


# --- Phase 3: speakers, scenes, signals --------------------------------------------------------------


def test_speakers_scenes_signals_and_plot(env: Any, media: dict[str, Path]) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    mid = done["media_id"]
    m = _ok(client.get(f"/api/v1/media/{mid}"))
    assert m["status"] == "ready", m

    speakers = _ok(client.get(f"/api/v1/media/{mid}/speakers"))
    assert [s["label"] for s in speakers] == ["S1", "S2"]
    renamed = _ok(client.patch(f"/api/v1/media/{mid}/speakers/S2", json={"display_name": "Guest"}))
    assert renamed["display_name"] == "Guest"
    assert client.patch(f"/api/v1/media/{mid}/speakers/S9", json={"display_name": "x"}).status_code == 404

    t = _ok(client.get(f"/api/v1/media/{mid}/transcript"))
    assert [s["speaker_label"] for s in t["segments"]] == ["S1", "S2", "S1", "S2"]
    assert {w["spk"] for w in t["segments"][1]["words"]} == {"S2"}

    scenes = _ok(client.get(f"/api/v1/media/{mid}/scenes"))
    assert len(scenes) == 1 and scenes[0]["start_ms"] == 0 and scenes[0]["has_keyframe"]
    kf = client.get(f"/api/v1/media/{mid}/scenes/0/keyframe")
    assert (
        kf.status_code == 200
        and kf.headers["content-type"] == "image/jpeg"
        and kf.content[:2] == bytes([0xFF, 0xD8])
    )

    signals = {s["kind"]: s for s in _ok(client.get(f"/api/v1/media/{mid}/signals"))}
    assert set(signals) == {"loudness", "energy", "speech_rate", "laughter", "excitement", "music"}
    assert signals["laughter"]["values"] == [0.0, 0.9, 0.1, 0.0]
    assert 38 <= len(signals["energy"]["values"]) <= 41  # 4 s at 100 ms
    assert signals["loudness"]["unit"] == "LUFS" and signals["loudness"]["stats"]["max"] > -40  # 440 Hz tone
    small = {s["kind"]: s for s in _ok(client.get(f"/api/v1/media/{mid}/signals", params={"max_points": 10}))}
    assert len(small["energy"]["values"]) <= 10 and small["energy"]["step"] >= 4

    plot = client.get(f"/api/v1/media/{mid}/signals/plot")
    assert plot.status_code == 200 and plot.headers["content-type"].startswith("image/svg+xml")
    assert plot.text.startswith("<svg") and "laughter" in plot.text and "speakers" in plot.text

    # Re-running diarization keeps the user's speaker names
    run = _ok(client.post(f"/api/v1/media/{mid}/analyze"))
    assert {s["status"] for s in run["stages"]} == {"skipped"}


def test_diarize_skipped_without_token(
    env: Any, media: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.config import get_settings

    monkeypatch.setattr(get_settings(), "hf_token", None)
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    m = _ok(client.get(f"/api/v1/media/{done['media_id']}"))
    assert m["status"] == "ready"  # a missing token must not fail the whole analysis
    d = {s["stage"]: s for s in m["latest_run"]["stages"]}["diarize"]
    assert d["status"] == "succeeded" and d["metrics"]["skipped_reason"] == "no_hf_token"
    assert _ok(client.get(f"/api/v1/media/{done['media_id']}/speakers")) == []


# --- Phase 4: highlights -------------------------------------------------------------------------------


def test_highlights_dry_run_run_and_candidates(
    env: Any, media: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from clipforge.engine.highlights.llm import FakeClient
    from clipforge.engine.highlights.run import HighlightConfig
    from clipforge.engine.highlights.validate import DurationBounds
    from clipforge.pipeline import stages_highlights

    def responder(system: str, user: str) -> str:
        if "Candidate as proposed" in user:
            return json.dumps(
                {
                    "keep": True,
                    "start_segment": 0,
                    "end_segment": 3,
                    "standalone_context": "self_contained",
                    "scores": {"hook": 8, "emotion": 7, "standalone": 9, "story": 6, "shareability": 8},
                    "title": "Test clip",
                    "hook_text": "Watch",
                    "description": "d",
                    "rationale": "r",
                }
            )
        return json.dumps(
            {
                "candidates": [
                    {
                        "start_segment": 0,
                        "end_segment": 3,
                        "category": "funny",
                        "hook": "h",
                        "description": "d",
                        "why_interesting": "w",
                        "standalone_context": "self_contained",
                        "setup_segment": None,
                        "scores": {"hook": 7, "emotion": 6, "standalone": 8, "story": 6, "shareability": 7},
                    },
                    {
                        "start_segment": 2,
                        "end_segment": 99,
                        "category": "story",
                        "hook": "h",
                        "description": "d",
                        "why_interesting": "w",
                        "standalone_context": "self_contained",
                        "setup_segment": None,
                        "scores": {"hook": 9, "emotion": 9, "standalone": 9, "story": 9, "shareability": 9},
                    },
                ]
            }
        )

    fake = FakeClient(responder)
    monkeypatch.setattr(stages_highlights, "client_factory", lambda backend, model: fake)
    # the 4 s fixture is far below the 12 s minimum clip length; relax bounds for this test
    monkeypatch.setattr(
        stages_highlights,
        "highlight_config",
        lambda rc: HighlightConfig(bounds=DurationBounds(min_ms=1000, max_ms=90_000)),
    )
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    mid = done["media_id"]

    est = _ok(client.post(f"/api/v1/media/{mid}/highlights", params={"dry_run": "true", "backend": "ollama"}))
    assert est["discover_requests"] == 1 and est["requests"] >= 2 and est["input_tokens"] > 0
    assert not fake.calls  # dry run never calls a model

    run = _ok(client.post(f"/api/v1/media/{mid}/highlights", params={"backend": "ollama"}))
    dispatcher.drain()
    run = _ok(client.get(f"/api/v1/pipeline-runs/{run['id']}"))
    assert run["status"] == "succeeded", run
    assert [q for s, q in dispatcher.executed[-3:]] == ["llm", "llm", "cpu"]

    cands = _ok(client.get(f"/api/v1/media/{mid}/candidates"))
    assert len(cands) == 1 and cands[0]["rank"] == 1 and cands[0]["title"] == "Test clip"
    assert cands[0]["start_ms"] >= 0 and cands[0]["end_ms"] > cands[0]["start_ms"]
    every = _ok(client.get(f"/api/v1/media/{mid}/candidates", params={"include_rejected": "true"}))
    bad = [c for c in every if any("s99 does not exist" in e for e in c["validation_errors"])]
    assert len(bad) == 1  # invalid IDs are kept with their reason, not dropped

    usage = _ok(client.get(f"/api/v1/media/{mid}/llm-usage"))
    assert usage[0]["backend"] == "ollama" and usage[0]["requests"] == len(fake.calls)
    calls_before = len(fake.calls)

    # same settings again -> every stage reused, candidates still served
    again = _ok(client.post(f"/api/v1/media/{mid}/highlights", params={"backend": "ollama"}))
    assert {s["status"] for s in again["stages"]} == {"skipped"}
    assert len(_ok(client.get(f"/api/v1/media/{mid}/candidates"))) == 1 and len(fake.calls) == calls_before


# --- Phase 5: clips & renders ------------------------------------------------------------------------


def test_clip_render_versions_and_rights_gate(env: Any, media: dict[str, Path]) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    mid = done["media_id"]

    assert (
        client.post(f"/api/v1/media/{mid}/clips", json={"start_ms": 3000, "end_ms": 2000}).status_code == 422
    )
    clip = _ok(
        client.post(f"/api/v1/media/{mid}/clips", json={"start_ms": 500, "end_ms": 3500, "title": "Test"}),
        201,
    )[0]
    r = _ok(
        client.post(
            f"/api/v1/clips/{clip['id']}/render", json={"preset": "shorts", "frame_mode": "blur_pad"}
        ),
        202,
    )
    assert r["status"] == "rendering"
    dispatcher.drain()
    clip = _ok(client.get(f"/api/v1/media/{mid}/clips"))[0]
    v = clip["latest_version"]
    assert clip["status"] == "rendered" and v["status"] == "ready", clip
    assert v["checks"]["ok"] and v["checks"]["size"] == "1080x1920" and v["checks"]["frames"] == 90
    assert v["render_spec"]["loudness"] is not None and v["render_spec"]["encoder"] == "x264"  # no GPU here

    video = client.get(f"/api/v1/clip-versions/{v['id']}/video")
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    part = client.get(f"/api/v1/clip-versions/{v['id']}/video", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100  # seekable in a <video> player
    assert client.get(f"/api/v1/clip-versions/{v['id']}/thumbnail").content[:2] == bytes([0xFF, 0xD8])

    # trim + re-render -> version 2, version 1 kept
    _ok(client.patch(f"/api/v1/clips/{clip['id']}", json={"start_ms": 1000, "title": "Trimmed"}))
    _ok(
        client.post(f"/api/v1/clips/{clip['id']}/render", json={"preset": "square", "frame_mode": "crop"}),
        202,
    )
    dispatcher.drain()
    versions = _ok(client.get(f"/api/v1/clips/{clip['id']}/versions"))
    assert [x["version"] for x in versions] == [2, 1] and versions[0]["checks"]["size"] == "1080x1080"

    # revoked rights block new renders
    _ok(client.post(f"/api/v1/rights-grants/{ids['grant']}/revoke"))
    assert client.post(f"/api/v1/clips/{clip['id']}/render", json={}).status_code == 403


# --- Phase 6: subtitles ----------------------------------------------------------------------------------


def test_subtitle_edit_render_and_export(env: Any, media: dict[str, Path]) -> None:
    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    mid = done["media_id"]
    clip = _ok(
        client.post(f"/api/v1/media/{mid}/clips", json={"start_ms": 0, "end_ms": 4000, "title": "T"}), 201
    )[0]

    subs = _ok(client.get(f"/api/v1/clips/{clip['id']}/subtitles"))
    assert not subs["edited"] and len(subs["words"]) == 12  # FakeTranscriber: 3 words per second
    words = subs["words"]
    words[0]["w"] = "EDITED"
    assert (
        client.put(f"/api/v1/clips/{clip['id']}/subtitles", json={"words": [words[1], words[0]]}).status_code
        == 422
    )  # out of order
    edited = _ok(client.put(f"/api/v1/clips/{clip['id']}/subtitles", json={"words": words}))
    assert edited["edited"] and edited["captions"][0]["text"].startswith("EDITED")

    _ok(
        client.post(
            f"/api/v1/clips/{clip['id']}/render",
            json={"subtitle_style": "karaoke", "watermark": "@me", "hook_text": False},
        ),
        202,
    )
    dispatcher.drain()
    v = _ok(client.get(f"/api/v1/media/{mid}/clips"))[0]["latest_version"]
    assert v["status"] == "ready", v
    sub = v["render_spec"]["extra"]["subtitles"]
    assert sub["style"] == "karaoke" and sub["words"][0]["w"] == "EDITED" and sub["watermark"] == "@me"
    srt = client.get(f"/api/v1/clip-versions/{v['id']}/subtitles.srt").text
    vtt = client.get(f"/api/v1/clip-versions/{v['id']}/subtitles.vtt").text
    assert srt.startswith("1\n00:00:00,") and "EDITED" in srt and vtt.startswith("WEBVTT")

    reset = _ok(client.delete(f"/api/v1/clips/{clip['id']}/subtitles"))
    assert not reset["edited"] and reset["words"][0]["w"] == "word"


# --- Phase 7: layouts + Premiere export -------------------------------------------------------------------


def test_layout_render_and_premiere_export(env: Any, media: dict[str, Path]) -> None:
    import io
    import zipfile

    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    mid = done["media_id"]
    clip = _ok(
        client.post(f"/api/v1/media/{mid}/clips", json={"start_ms": 0, "end_ms": 3000, "title": "Lay"}), 201
    )[0]
    bad = client.post(f"/api/v1/clips/{clip['id']}/render", json={"frame_mode": "facecam_split"})
    assert bad.status_code == 422  # needs a facecam region
    assert (
        client.post(
            f"/api/v1/clips/{clip['id']}/render",
            json={"frame_mode": "facecam_split", "facecam": [0.9, 0, 0.3, 0.3]},
        ).status_code
        == 422
    )
    _ok(
        client.post(
            f"/api/v1/clips/{clip['id']}/render", json={"frame_mode": "facecam_split", "facecam": "top_right"}
        ),
        202,
    )
    dispatcher.drain()
    v = _ok(client.get(f"/api/v1/media/{mid}/clips"))[0]["latest_version"]
    assert v["status"] == "ready" and v["render_spec"]["extra"]["layout"]["facecam"] == [
        0.73,
        0.0,
        0.27,
        0.32,
    ]

    r = client.get(f"/api/v1/media/{mid}/export/premiere", params={"source_path": "C:/Videos/cfr.mp4"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert {"clipforge_project.xml", "README.txt", "captions/01 Lay.srt"} <= set(z.namelist())
    xml = z.read("clipforge_project.xml").decode()
    assert "file://localhost/C:/Videos/cfr.mp4" in xml and "Lay (9x16)" in xml


# --- Phase 8: review ---------------------------------------------------------------------------------------


def test_review_approve_reject_audit_waveform_bulk(env: Any, media: dict[str, Path]) -> None:
    import io
    import zipfile

    client, dispatcher, _ = env
    ids = _setup(client)
    done = _upload_all(client, ids, media["mp4"])
    dispatcher.drain()
    mid = done["media_id"]
    a, b = (
        _ok(
            client.post(f"/api/v1/media/{mid}/clips", json={"start_ms": 0, "end_ms": 2000, "title": "A"}), 201
        )[0],
        _ok(
            client.post(f"/api/v1/media/{mid}/clips", json={"start_ms": 1000, "end_ms": 3500, "title": "B"}),
            201,
        )[0],
    )
    assert client.post(f"/api/v1/clips/{a['id']}/approve").status_code == 409  # not rendered yet
    _ok(client.post(f"/api/v1/clips/{a['id']}/render", json={"frame_mode": "blur_pad"}), 202)
    dispatcher.drain()
    _ok(client.patch(f"/api/v1/clips/{a['id']}", json={"title": "A2"}))
    _ok(client.post(f"/api/v1/clips/{a['id']}/render", json={"frame_mode": "crop"}), 202)  # regenerate
    dispatcher.drain()
    versions = _ok(client.get(f"/api/v1/clips/{a['id']}/versions"))
    assert [v["version"] for v in versions] == [2, 1] and all(v["status"] == "ready" for v in versions)
    assert client.get(f"/api/v1/clip-versions/{versions[1]['id']}/video").status_code == 200  # old one kept

    approved = _ok(client.post(f"/api/v1/clips/{a['id']}/approve"))
    assert approved["review"] == "approved" and approved["reviewed_at"]
    assert client.post(f"/api/v1/clips/{b['id']}/reject", json={"reason": ""}).status_code == 422
    rejected = _ok(client.post(f"/api/v1/clips/{b['id']}/reject", json={"reason": "no payoff"}))
    assert rejected["review"] == "rejected" and rejected["reject_reason"] == "no payoff"
    assert _ok(client.post(f"/api/v1/clips/{b['id']}/reopen"))["review"] == "pending"

    trail = _ok(client.get("/api/v1/audit", params={"entity_id": a["id"]}))
    assert [e["action"] for e in trail][::-1] == [
        "clip.create",
        "clip.render",
        "clip.update",
        "clip.render",
        "clip.approve",
    ]
    assert trail[0]["data"]["version_id"] == approved["current_version_id"]
    assert [e["action"] for e in _ok(client.get("/api/v1/audit", params={"entity_id": b["id"]}))][:2] == [
        "clip.reopen",
        "clip.reject",
    ]

    wf = _ok(client.get(f"/api/v1/media/{mid}/waveform", params={"from_ms": 0, "to_ms": 4000, "points": 100}))
    assert len(wf["peaks"]) == 100 and max(wf["peaks"]) > 0.05  # the fixture's 440 Hz tone
    prev = client.get(f"/api/v1/media/{mid}/preview", headers={"Range": "bytes=0-9"})
    assert prev.status_code == 206

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/media/{mid}/approved.zip").content))
    assert sorted(z.namelist()) == ["01 A2.mp4", "01 A2.srt", "clips.csv"] or sorted(z.namelist()) == [
        "01 A2.mp4",
        "clips.csv",
    ]
    assert "A2" in z.read("clips.csv").decode()
