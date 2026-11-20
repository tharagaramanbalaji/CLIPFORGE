from __future__ import annotations

import wave
from pathlib import Path

from clipforge.engine.diarize.assign import (
    SpeakerTurn,
    assign_speakers,
    best_label_agreement,
    normalize_labels,
)
from clipforge.engine.signals.audio import energy_series, parse_ebur128, speech_rate_series
from clipforge.engine.signals.events import class_indices


def test_normalize_labels_orders_by_first_appearance() -> None:
    turns = [
        SpeakerTurn(5000, 6000, "SPEAKER_07"),
        SpeakerTurn(0, 1000, "SPEAKER_02"),
        SpeakerTurn(2000, 1000, "bad"),
    ]
    assert [(t.start_ms, t.speaker) for t in normalize_labels(turns)] == [(0, "S1"), (5000, "S2")]


def test_assign_words_by_overlap_and_nearest_turn() -> None:
    turns = [SpeakerTurn(0, 1000, "S1"), SpeakerTurn(1000, 2000, "S2"), SpeakerTurn(3000, 4000, "S1")]
    segs = [
        {"words": [{"s": 100, "e": 400}, {"s": 900, "e": 1300}]},  # 2nd word overlaps S2 more (300 vs 100)
        {"words": [{"s": 2100, "e": 2300}]},  # 100 ms after S2 ends -> nearest is S2
        {"words": [{"s": 2600, "e": 2700}, {"s": 3100, "e": 3500}]},  # 1st: 300 ms before S1 turn
        {"words": [{"s": 9000, "e": 9100}]},  # nothing near -> None
    ]
    a = assign_speakers(segs, turns)
    assert a.word_speakers == [["S1", "S2"], ["S2"], ["S1", "S1"], [None]]
    assert a.segment_speakers == ["S2", "S2", "S1", None]  # S2 word is longer (400 ms vs 300)
    assert a.totals_ms == {"S1": 2000, "S2": 1000}
    assert a.word_coverage == 5 / 6


def test_label_agreement_is_permutation_invariant() -> None:
    truth = ["A", "A", "B", "B", "A"]
    assert best_label_agreement(truth, ["S2", "S2", "S1", "S1", "S2"]) == 1.0
    assert best_label_agreement(truth, ["S1", "S1", "S1", "S2", None]) == 3 / 5


def test_parse_ebur128_metadata() -> None:
    out = "\n".join(
        [
            "frame:0    pts:0       pts_time:0",
            "lavfi.r128.M=-120.691",
            "frame:1    pts:1600    pts_time:0.1",
            "lavfi.r128.M=-30.5",
            "frame:3    pts:4800    pts_time:0.3",
            "lavfi.r128.M=-18",
            "frame:4    pts:6400    pts_time:0.4",
            "lavfi.r128.M=-inf",
        ]
    )
    # gap at 0.2 s repeats the previous value; silence and -inf clamp to -90
    assert parse_ebur128(out) == [-90.0, -30.5, -30.5, -18.0, -90.0]


def test_speech_rate_window() -> None:
    starts = [i * 500 for i in range(20)]  # 2 words/s for 10 s
    s = speech_rate_series(starts, 10_000)
    assert s.hop_ms == 1000 and len(s.values) == 10
    assert s.values[5] == 2.0 and s.values[0] < 2.0  # edge window is half outside


def test_energy_series_levels(tmp_path: Path) -> None:
    import math
    import struct

    sr = 16000
    samples = [0] * sr + [int(16000 * math.sin(2 * math.pi * 440 * i / sr)) for i in range(sr)]
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    s = energy_series(path)
    assert len(s.values) == 20
    assert s.values[0] == -90.0 and -10 < s.values[15] < -6  # half-scale sine is about -9 dBFS RMS


def test_audioset_groups_map_to_indices() -> None:
    idx = class_indices({0: "Speech", 1: "Laughter", 2: "Music", 3: "Cheering", 4: "Giggle"})
    assert idx == {"laughter": [1, 4], "excitement": [3], "music": [2]}
