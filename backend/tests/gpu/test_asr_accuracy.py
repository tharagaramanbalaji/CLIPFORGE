"""Real-model ASR tests. Run on the GPU worker:  docker compose exec worker-gpu pytest -m gpu -s
(CPU fallback test: docker compose exec api pytest -m "media and slow" -s -k cpu)"""

from __future__ import annotations

import difflib
import re
import statistics
from pathlib import Path

import pytest

from clipforge.engine.asr.base import TranscriptResult
from clipforge.engine.asr.whisperx_backend import WhisperXTranscriber
from clipforge.pipeline.asr_config import asr_config_for_this_host
from tests.fixtures_speech import HELDOUT_SENTENCES, SENTENCES, TimedWord, make_timed_speech

TOLERANCE_MS = 100


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def timing_errors(result: TranscriptResult, truth: list[TimedWord]) -> tuple[list[int], list[int], int]:
    """Match recognized words to ground truth by text (sequence alignment), return start/end errors."""
    hyp = [w for s in result.segments for w in s.words]
    a = [_norm(t.word) for t in truth]
    b = [_norm(w.w) for w in hyp]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    start_err, end_err = [], []
    for block in sm.get_matching_blocks():
        for k in range(block.size):
            t, h = truth[block.a + k], hyp[block.b + k]
            start_err.append(abs(h.s - t.start_ms))
            end_err.append(abs(h.e - t.end_ms))
    return start_err, end_err, len(a)


def _report(name: str, result: TranscriptResult, truth: list[TimedWord]) -> tuple[float, float]:
    start_err, end_err, total = timing_errors(result, truth)
    matched = len(start_err) / total
    within = sum(e <= TOLERANCE_MS for e in start_err) / max(1, len(start_err))
    p90 = statistics.quantiles(start_err, n=10)[-1] if len(start_err) >= 10 else max(start_err, default=0)
    print(
        f"\n[{name}] words matched {len(start_err)}/{total} ({matched:.0%}); start error median "
        f"{statistics.median(start_err):.0f} ms, p90 {p90:.0f} ms, max {max(start_err)} ms; "
        f"{within:.0%} within ±{TOLERANCE_MS} ms; end error median {statistics.median(end_err):.0f} ms; "
        f"metrics {result.metrics}"
    )
    return matched, within


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[TimedWord]]:
    return make_timed_speech(tmp_path_factory.mktemp("speech"))


@pytest.mark.gpu
@pytest.mark.parametrize(
    ("name", "sentences", "seed"), [("calibration", SENTENCES, 7), ("held-out", HELDOUT_SENTENCES, 99)]
)
def test_word_timings_within_100ms_on_gpu(tmp_path: Path, name: str, sentences: list[str], seed: int) -> None:
    wav, truth = make_timed_speech(tmp_path, sentences, seed=seed)
    cfg = asr_config_for_this_host()
    assert cfg.device == "cuda", "run this on the GPU worker"
    result = WhisperXTranscriber().transcribe(wav, cfg)
    matched, within = _report(f"gpu {cfg.model} {name}", result, truth)
    assert matched >= 0.9
    assert within >= 0.9
    gpu = result.metrics["gpu_asr"]
    assert gpu["leaked_mib"] is not None and gpu["leaked_mib"] < 300, gpu  # models released


@pytest.mark.media
@pytest.mark.slow
def test_cpu_fallback_transcribes(speech: tuple[Path, list[TimedWord]]) -> None:
    cfg = asr_config_for_this_host(force_device="cpu")
    assert cfg.device == "cpu"
    result = WhisperXTranscriber().transcribe(speech[0], cfg)
    matched, within = _report("cpu " + cfg.model, result, speech[1])
    assert matched >= 0.8 and within >= 0.8


def test_timing_error_matching_logic() -> None:
    from clipforge.engine.asr.base import Segment, Word

    truth = [TimedWord("hello", 0, 300), TimedWord("big", 350, 500), TimedWord("world", 520, 900)]
    seg = Segment(0, 10, 950, "Hello, world!", (Word("Hello,", 10, 310), Word("world!", 560, 950)))
    start, end, total = timing_errors(TranscriptResult("x", "m", "en", (seg,)), truth)
    assert (start, end, total) == ([10, 40], [10, 50], 3)
