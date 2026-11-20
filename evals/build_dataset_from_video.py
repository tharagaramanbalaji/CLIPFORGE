"""Turn a video file + hand-marked moments into an eval dataset, using the real local analysis stages
(16 kHz audio, WhisperX transcript, pyannote speakers, audio-event/energy peaks, scene cuts). No Claude.

    docker compose exec worker-gpu python /evals/build_dataset_from_video.py /data/tmp/video.mp4 \
        --name eafc25_seasons1 --labels "1:32-1:50,3:30-3:42" --context "FIFA/EA FC YouTuber"
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app/src")

from clipforge.config import get_settings  # noqa: E402
from clipforge.engine.asr.whisperx_backend import WhisperXTranscriber  # noqa: E402
from clipforge.engine.diarize.assign import assign_speakers  # noqa: E402
from clipforge.engine.diarize.pyannote_backend import DiarizeConfig, diarize  # noqa: E402
from clipforge.engine.highlights.windowing import find_peaks  # noqa: E402
from clipforge.engine.media.audio import extract_audio  # noqa: E402
from clipforge.engine.media.probe import probe_media  # noqa: E402
from clipforge.engine.scenes.detect import SceneConfig, detect_scenes  # noqa: E402
from clipforge.engine.signals.audio import energy_series  # noqa: E402
from clipforge.engine.signals.events import EventsConfig, detect_events  # noqa: E402
from clipforge.pipeline.asr_config import asr_config_for_this_host  # noqa: E402


def ts(text: str) -> int:
    parts = [float(p) for p in text.strip().split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return int(secs * 1000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--name", required=True)
    ap.add_argument("--labels", required=True, help='"m:ss-m:ss,m:ss-m:ss,..."')
    ap.add_argument("--context", default="")
    args = ap.parse_args()

    video = Path(args.video)
    duration = probe_media(video).duration_ms
    tmp = Path(tempfile.mkdtemp())
    wav = tmp / "audio.wav"
    extract_audio(get_settings().ffmpeg_bin, video, wav, duration)
    print("audio ok", flush=True)

    tr = WhisperXTranscriber().transcribe(wav, asr_config_for_this_host())
    print(f"transcript: {len(tr.segments)} segments, {tr.word_count} words, {tr.metrics.get('realtime_factor')}x",
          flush=True)
    segs = [{"words": [w.as_dict() for w in s.words]} for s in tr.segments]
    speakers: list[str | None] = [None] * len(tr.segments)
    tok = get_settings().hf_token
    if tok and tok.get_secret_value():
        turns, dm = diarize(wav, DiarizeConfig(device="cuda"), tok.get_secret_value())
        speakers = assign_speakers(segs, turns).segment_speakers
        print(f"speakers: {dm['speakers']}", flush=True)

    peaks = []
    series, _ = detect_events(wav, EventsConfig(device="cuda"))
    for s in series:
        if s.kind in ("laughter", "excitement"):
            peaks += find_peaks(s.values, s.hop_ms, s.kind, threshold=0.5)
    energy = energy_series(wav)
    if energy.values:
        vals = sorted(energy.values)
        thr = max(vals[int(0.995 * (len(vals) - 1))], -20.0)
        peaks += find_peaks(energy.values, energy.hop_ms, "loud", threshold=thr, min_gap_ms=30_000, limit=100)
    scenes, _ = detect_scenes(video, SceneConfig())
    print(f"peaks: {len(peaks)}, scene cuts: {len(scenes) - 1}", flush=True)

    labels = []
    for item in args.labels.split(","):
        a, b = item.split("-")
        labels.append({"start_ms": ts(a), "end_ms": ts(b), "category": "marked"})
    data = {
        "name": args.name, "duration_ms": duration, "creator_context": args.context,
        "segments": [{"idx": s.idx, "start_ms": s.start_ms, "end_ms": s.end_ms, "speaker": spk, "text": s.text,
                      "words": [{"w": w.w, "s": w.s, "e": w.e} for w in s.words]}
                     for s, spk in zip(tr.segments, speakers, strict=True)],
        "labels": labels,
        "peaks": [{"ms": p.ms, "kind": p.kind, "value": p.value} for p in peaks],
        "cuts": [s.start_ms for s in scenes[1:]],
    }
    out = Path("/evals/datasets") / f"{args.name}.json"
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
