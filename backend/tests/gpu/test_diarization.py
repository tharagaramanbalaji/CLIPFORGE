"""Real-model speaker diarization accuracy on a two-voice synthetic dialogue (needs CLIPFORGE_HF_TOKEN),
plus an audio-event model smoke test. Run on the GPU worker:
    docker compose exec worker-gpu pytest -m gpu -s tests/gpu
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from clipforge.config import get_settings
from clipforge.engine.asr.whisperx_backend import WhisperXTranscriber
from clipforge.engine.diarize.assign import assign_speakers, best_label_agreement
from clipforge.engine.diarize.pyannote_backend import DiarizeConfig, diarize
from clipforge.engine.signals.events import EventsConfig, detect_events
from clipforge.pipeline.asr_config import asr_config_for_this_host
from tests.fixtures_speech import make_dialogue
from tests.gpu.test_asr_accuracy import _norm

pytestmark = pytest.mark.gpu


def _token() -> str:
    tok = get_settings().hf_token
    value = tok.get_secret_value() if tok else ""
    if not value:
        pytest.skip("CLIPFORGE_HF_TOKEN not set")
    return value


@pytest.mark.parametrize("known_count", [False, True])
def test_two_speaker_word_agreement(tmp_path: Path, known_count: bool) -> None:
    token = _token()
    wav, truth = make_dialogue(tmp_path)
    result = WhisperXTranscriber().transcribe(wav, asr_config_for_this_host())
    n = 2 if known_count else None
    turns, metrics = diarize(wav, DiarizeConfig(device="cuda", min_speakers=n, max_speakers=n), token)
    segs = [{"words": [w.as_dict() for w in s.words]} for s in result.segments]
    assignment = assign_speakers(segs, turns)
    hyp_words = [w for s in result.segments for w in s.words]
    hyp_spk = [spk for row in assignment.word_speakers for spk in row]
    sm = difflib.SequenceMatcher(
        a=[_norm(t.word) for t in truth], b=[_norm(w.w) for w in hyp_words], autojunk=False
    )
    t_lab: list[str] = []
    p_lab: list[str | None] = []
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            t_lab.append(truth[b.a + k].speaker)
            p_lab.append(hyp_spk[b.b + k])
    agreement = best_label_agreement(t_lab, p_lab)
    print(
        f"\n[diarize known_count={known_count}] speakers found {metrics['speakers']}, matched words "
        f"{len(t_lab)}/{len(truth)}, word agreement {agreement:.1%}, coverage {assignment.word_coverage:.1%}, "
        f"metrics {metrics}"
    )
    assert agreement >= 0.85
    assert metrics["gpu"]["leaked_mib"] is not None and metrics["gpu"]["leaked_mib"] < 300


def test_audio_events_run_and_release_memory(tmp_path: Path) -> None:
    wav, _ = make_dialogue(tmp_path)
    series, metrics = detect_events(wav, EventsConfig(device="cuda"))
    by = {s.kind: s for s in series}
    print(f"\n[events] {metrics} " + " ".join(f"{k}: max {max(v.values):.2f}" for k, v in by.items()))
    assert set(by) == {"laughter", "excitement", "music"}
    assert all(len(s.values) == metrics["windows"] for s in series)
    assert max(by["laughter"].values) < 0.5  # plain speech: no laughter
    assert metrics["gpu"]["leaked_mib"] is not None and metrics["gpu"]["leaked_mib"] < 300
