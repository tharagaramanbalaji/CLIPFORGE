"""Diagnostics for word-timing accuracy: signed start/end errors per alignment model.

docker compose exec worker-gpu python scripts/align_probe.py [--align-model NAME ...]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures_speech import make_timed_speech
from tests.gpu.test_asr_accuracy import _norm

from clipforge.engine.asr.postprocess import build_segments
from clipforge.pipeline.asr_config import asr_config_for_this_host


def main() -> None:
    import difflib

    import whisperx

    ap = argparse.ArgumentParser()
    ap.add_argument("--align-model", action="append", default=[])
    args = ap.parse_args()
    wav, truth = make_timed_speech(Path(tempfile.mkdtemp()))
    cfg = asr_config_for_this_host()
    audio = whisperx.load_audio(str(wav))
    model = whisperx.load_model(
        cfg.model,
        cfg.device,
        compute_type=cfg.compute_type,
        language="en",
        asr_options={"condition_on_previous_text": False},
    )
    raw = model.transcribe(audio, batch_size=16, language="en")["segments"]
    del model
    for name in [None, *args.align_model]:
        am, meta = whisperx.load_align_model(language_code="en", device=cfg.device, model_name=name)
        segs = build_segments(whisperx.align(raw, am, meta, audio, cfg.device)["segments"])
        hyp = [w for s in segs for w in s.words]
        sm = difflib.SequenceMatcher(
            a=[_norm(t.word) for t in truth], b=[_norm(w.w) for w in hyp], autojunk=False
        )
        ds, de = [], []
        for b in sm.get_matching_blocks():
            for k in range(b.size):
                ds.append(hyp[b.b + k].s - truth[b.a + k].start_ms)
                de.append(hyp[b.b + k].e - truth[b.a + k].end_ms)
        within = sum(abs(d) <= 100 for d in ds) / len(ds)
        print(
            f"{name or 'default'}: n={len(ds)} start signed mean {statistics.mean(ds):+.0f} median "
            f"{statistics.median(ds):+.0f} | end signed median {statistics.median(de):+.0f} | "
            f"start within100 {within:.0%} | worst {sorted(ds, key=abs)[-5:]}"
        )
        del am


if __name__ == "__main__":
    main()
