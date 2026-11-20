"""Integration fixtures: a throwaway `<db>_test` database migrated with Alembic, a temp data dir,
small upload chunks, an in-process task dispatcher, and recorded events.

Run inside the stack:  docker compose exec api pytest -m "integration or media"
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_BASE_URL = os.environ.get(
    "CLIPFORGE_DATABASE_URL", "postgresql+psycopg://clipforge:clipforge@localhost:5432/clipforge"
)
_TEST_URL = make_url(_BASE_URL).set(database=f"{make_url(_BASE_URL).database}_test")
_DATA_DIR = Path(tempfile.mkdtemp(prefix="clipforge-it-"))

# Must happen before anything calls get_settings()/get_engine() for these tests.
os.environ["CLIPFORGE_DATABASE_URL"] = _TEST_URL.render_as_string(hide_password=False)
os.environ["CLIPFORGE_DATA_DIR"] = str(_DATA_DIR)
os.environ["CLIPFORGE_UPLOAD_CHUNK_SIZE"] = str(256 * 1024)
os.environ["CLIPFORGE_VIDEO_ENCODER"] = "x264"
os.environ["CLIPFORGE_LOG_JSON"] = "false"

from clipforge.config import get_settings  # noqa: E402
from clipforge.db import session as db_session  # noqa: E402

get_settings.cache_clear()
db_session.get_engine.cache_clear()
db_session._session_factory.cache_clear()


@pytest.fixture(scope="session")
def database() -> Iterator[None]:
    admin = create_engine(make_url(_BASE_URL), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_URL.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{_TEST_URL.database}"'))
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(cfg, "head")
    yield
    db_session.get_engine().dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_URL.database}" WITH (FORCE)'))
    admin.dispose()


@dataclass
class InlineDispatcher:
    """Collects dispatched stage runs; drain() executes them in-process like a worker would."""

    queued: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    executed: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, sr: object, queue: str) -> str:
        self.queued.append((sr.id, queue))  # type: ignore[attr-defined]
        return f"inline-{len(self.queued)}"

    def drain(self, max_steps: int = 50) -> None:
        from clipforge.db.models import StageRun
        from clipforge.db.session import new_session
        from clipforge.pipeline.handlers import HANDLERS
        from clipforge.pipeline.orchestrator import Outcome, execute_stage

        for _ in range(max_steps):
            if not self.queued:
                return
            stage_run_id, queue = self.queued.pop(0)
            with new_session() as s:
                stage = s.get(StageRun, stage_run_id)
                self.executed.append((stage.stage.value if stage else "?", queue))
                result = execute_stage(s, stage_run_id, self, HANDLERS, new_session)
            if result.outcome is Outcome.RETRY:
                self.queued.append((stage_run_id, queue))
        raise AssertionError("pipeline did not settle")


@pytest.fixture
def clean_db(database: None) -> Iterator[None]:
    from clipforge.db.base import Base
    from clipforge.db.session import get_engine

    yield
    with get_engine().begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} CASCADE"))


@pytest.fixture
def data_dir() -> Path:
    return _DATA_DIR


class FakeTranscriber:
    """Deterministic stand-in for WhisperX: one 3-word segment per second of audio (no models needed)."""

    calls: int = 0

    def transcribe(self, audio_path: Path, cfg: object, on_progress: object = None) -> object:
        import wave

        from clipforge.engine.asr.base import Segment, TranscriptResult, Word

        FakeTranscriber.calls += 1
        with wave.open(str(audio_path), "rb") as w:
            assert w.getframerate() == 16_000 and w.getnchannels() == 1, "audio_extract must give 16 kHz mono"
            seconds = w.getnframes() // w.getframerate()
        segs = []
        for i in range(seconds):
            base = i * 1000
            words = (
                Word("word", base, base + 200, 0.9),
                Word(f"number{i}", base + 250, base + 600, 0.8),
                Word("end.", base + 650, base + 900, 0.95),
            )
            segs.append(Segment(i, base, base + 900, f"word number{i} end.", words, 0.88))
        return TranscriptResult("fake", "fake-model", "en", tuple(segs), {"audio_s": seconds})


@pytest.fixture(autouse=True)
def fake_transcriber(monkeypatch: pytest.MonkeyPatch) -> type[FakeTranscriber]:
    from clipforge.pipeline import stages

    FakeTranscriber.calls = 0
    monkeypatch.setattr(stages, "transcriber_factory", FakeTranscriber)
    return FakeTranscriber


def fake_diarize(wav: Path, cfg: object, token: str | None) -> tuple[list[object], dict[str, object]]:
    """Alternating speakers every second: S1 on even seconds, S2 on odd ones (matches FakeTranscriber)."""
    import wave

    from clipforge.engine.diarize.assign import SpeakerTurn

    with wave.open(str(wav), "rb") as w:
        seconds = w.getnframes() // w.getframerate()
    turns = [SpeakerTurn(i * 1000, i * 1000 + 950, "S1" if i % 2 == 0 else "S2") for i in range(seconds)]
    return turns, {"speakers": 2, "turns": len(turns)}


def fake_events(wav: Path, device: str) -> tuple[list[object], dict[str, object]]:
    from clipforge.engine.signals.audio import Series

    n = 4
    return [
        Series("laughter", 1000, [0.0, 0.9, 0.1, 0.0][:n], "probability"),
        Series("excitement", 1000, [0.1] * n, "probability"),
        Series("music", 1000, [0.0] * n, "probability"),
    ], {"windows": n}


@pytest.fixture(autouse=True)
def fake_analysis_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from clipforge.pipeline import stages_analysis

    monkeypatch.setattr(stages_analysis, "diarize_fn", fake_diarize)
    monkeypatch.setattr(stages_analysis, "events_fn", fake_events)
    monkeypatch.setattr(get_settings(), "hf_token", SecretStr("hf_fake_for_tests"))
