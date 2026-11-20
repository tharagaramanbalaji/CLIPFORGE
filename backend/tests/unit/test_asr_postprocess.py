from __future__ import annotations

from clipforge.engine.asr.base import Word
from clipforge.engine.asr.postprocess import build_segments, fill_word_times, is_repetitive, lead_word_starts
from clipforge.engine.media.audio import build_audio_args


def test_missing_word_times_are_interpolated_between_neighbours() -> None:
    raw = [
        {"word": "It", "start": 1.0, "end": 1.1, "score": 0.9},
        {"word": "cost"},  # no timing
        {"word": "$40", "start": None},
        {"word": "today", "start": 1.5, "end": 1.9, "score": 0.8},
    ]
    words, interpolated = fill_word_times(raw, 1000, 2000)
    assert interpolated
    assert [(w.w, w.s, w.e) for w in words] == [
        ("It", 1000, 1100),
        ("cost", 1100, 1300),
        ("$40", 1300, 1500),
        ("today", 1500, 1900),
    ]
    assert words[1].p is None and words[0].p == 0.9


def test_leading_untimed_words_use_segment_start() -> None:
    words, _ = fill_word_times([{"word": "uh"}, {"word": "yes", "start": 2.0, "end": 2.2}], 1800, 2300)
    assert (words[0].s, words[0].e) == (1800, 2000)


def test_build_segments_ms_monotonic_and_flags() -> None:
    raw = [
        {
            "text": " Hello  there. ",
            "start": 0.5,
            "end": 1.2,
            "words": [
                {"word": "Hello", "start": 0.5, "end": 0.8, "score": 0.9},
                {"word": "there.", "start": 0.85, "end": 1.2, "score": 0.7},
            ],
        },
        {"text": "", "start": 1.3, "end": 1.4},  # dropped
        {
            "text": "overlap",
            "start": 1.1,
            "end": 1.6,
            "words": [{"word": "overlap", "start": 1.1, "end": 1.6, "score": 0.8}],
        },
        {
            "text": " ".join(["thank you"] * 6),
            "start": 2.0,
            "end": 6.0,
            "words": [
                {"word": w, "start": 2 + i * 0.3, "end": 2.2 + i * 0.3}
                for i, w in enumerate(("thank you " * 6).split())
            ],
        },
    ]
    segs = build_segments(raw)
    assert [s.idx for s in segs] == [0, 1, 2]
    assert (segs[0].start_ms, segs[0].end_ms, segs[0].text, segs[0].confidence) == (
        500,
        1200,
        "Hello there.",
        0.8,
    )
    assert segs[1].start_ms >= segs[0].end_ms  # overlap pushed forward
    assert "repetitive" in segs[2].flags


def test_segment_without_word_alignment_still_gets_word_times() -> None:
    segs = build_segments([{"text": "one two three four", "start": 0.0, "end": 2.0}])
    assert [(w.s, w.e) for w in segs[0].words] == [(0, 500), (500, 1000), (1000, 1500), (1500, 2000)]
    assert "interpolated_words" in segs[0].flags


def test_is_repetitive() -> None:
    mk = lambda t: [Word(w, 0, 1) for w in t.split()]  # noqa: E731
    assert is_repetitive(mk("thank you " * 5))
    assert not is_repetitive(mk("we found the hidden door behind the old library"))


def test_audio_args_are_16k_mono_pcm() -> None:
    from pathlib import Path

    args = build_audio_args(Path("in.mp4"), Path("out.wav"))
    assert args[args.index("-ar") + 1] == "16000" and args[args.index("-ac") + 1] == "1"
    assert "pcm_s16le" in args


def test_start_lead_never_overlaps_previous_word_or_moves_later() -> None:
    words = [Word("a", 1000, 1200), Word("b", 1220, 1400), Word("c", 1600, 1800)]
    led = lead_word_starts(words, 50, floor_ms=980)
    assert [(w.s, w.e) for w in led] == [(980, 1200), (1200, 1400), (1550, 1800)]
    assert lead_word_starts([Word("x", 20, 100)], 50, 0)[0].s == 0
