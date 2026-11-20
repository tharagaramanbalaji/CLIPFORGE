"""ASR benchmark / long-audio OOM check. Run on the GPU worker:

    docker compose exec worker-gpu python scripts/bench_asr.py --minutes 60

Synthesizes N minutes of speech, transcribes it with the configured model, and prints realtime factor,
peak device memory and whether memory went back to the starting level (models released).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures_speech import make_long_speech

from clipforge.engine.asr.whisperx_backend import WhisperXTranscriber
from clipforge.pipeline.asr_config import asr_config_for_this_host


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--device", choices=["cuda", "cpu"], default=None)
    ap.add_argument("--work", default="/data/tmp/bench")
    args = ap.parse_args()

    t = time.monotonic()
    wav = make_long_speech(Path(args.work), args.minutes)
    print(f"synthesized {args.minutes:g} min in {time.monotonic() - t:.0f}s -> {wav}", flush=True)
    cfg = asr_config_for_this_host(force_device=args.device)
    print(f"config: {cfg.as_dict()}", flush=True)
    last = [0.0]

    def progress(f: float) -> bool:
        if f - last[0] >= 0.1 or f >= 0.97:
            print(f"  progress {f:.0%}", flush=True)
            last[0] = f
        return True

    result = WhisperXTranscriber().transcribe(wav, cfg, progress)
    print(json.dumps(result.metrics, indent=2))
    leaked = (result.metrics.get("gpu_asr") or {}).get("leaked_mib")
    print(
        f"RESULT segments={len(result.segments)} words={result.word_count} "
        f"rtf={result.metrics['realtime_factor']}x leaked_mib={leaked}"
    )
    wav.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
