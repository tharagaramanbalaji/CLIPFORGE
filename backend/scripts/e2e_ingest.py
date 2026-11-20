"""End-to-end ingest smoke test against a running stack (real API, Celery workers, GPU).

Run inside the api container on a FRESH install (it registers the owner account):
    docker compose exec api python scripts/e2e_ingest.py --big-gb 5

1. Uploads a VFR 1080p clip and waits until it is ready (checks NVENC was used, VFR normalized).
2. Builds a ~N GB valid video, uploads it, aborts mid-chunk and then kills the "client" partway,
   resumes from the server's chunk list, and verifies the reassembled sha256 and final metadata.
Refuses to run if a user already exists, so it never touches real data.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("E2E_BASE", "http://localhost:8000/api/v1")


def ok(r: httpx.Response, code: int = 200) -> Any:
    if r.status_code != code:
        sys.exit(f"FAIL {r.request.method} {r.request.url.path}: {r.status_code} {r.text[:500]}")
    return r.json()


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_vfr(path: Path) -> None:
    """2-min VFR 1080p60 clip with a synthesized speech track (so transcription has something to find)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.fixtures_speech import make_long_speech

    speech = make_long_speech(path.parent / "speech", 2)
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=60",
        "-i",
        str(speech),
        "-t",
        "120",
        "-vf",
        "setpts='(N+floor(N/4))/(60*TB)'",
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )


def make_big(path: Path, target_gb: float, work: Path) -> None:
    """Lossless x264 of noise is nearly incompressible -> GBs fast; stream-copy concat to the target size."""
    seg = work / "seg.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=30,noise=alls=80:allf=t",
        "-f",
        "lavfi",
        "-i",
        "sine=f=220:r=48000",
        "-t",
        "10",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-qp",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(seg),
    )
    repeats = max(1, round(target_gb * 1024**3 / seg.stat().st_size))
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{seg}'\n" for _ in range(repeats)))
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(path))
    seg.unlink()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(16 * 1024 * 1024):
            h.update(b)
    return h.hexdigest()


def chunk(path: Path, up: dict[str, Any], n: int) -> bytes:
    with path.open("rb") as f:
        f.seek(n * up["chunk_size"])
        return f.read(up["chunk_size"])


def put(c: httpx.Client, path: Path, up: dict[str, Any], n: int) -> dict[str, Any]:
    data = chunk(path, up, n)
    return ok(
        c.put(
            f"/uploads/{up['upload_id']}/chunks/{n}",
            content=data,
            headers={"X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
        )
    )


def wait_ready(c: httpx.Client, media_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        m = ok(c.get(f"/media/{media_id}"))
        run = m["latest_run"] or {}
        state = " ".join(f"{s['stage']}={s['status']}:{s['progress']:.0%}" for s in run.get("stages", []))
        if state != last:
            print(f"   {m['status']:<10} {state}", flush=True)
            last = state
        if m["status"] in ("ready", "failed"):
            return m
        time.sleep(2)
    sys.exit(f"FAIL media {media_id} not ready after {timeout_s}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--big-gb", type=float, default=5.0)
    ap.add_argument("--work", default="/data/tmp/e2e")
    args = ap.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    c = httpx.Client(base_url=BASE, timeout=120)
    password = secrets.token_urlsafe(18)
    r = c.post(
        "/auth/register", json={"email": "e2e@example.com", "password": password, "display_name": "E2E"}
    )
    if r.status_code == 403:
        sys.exit("Refusing to run: an owner account already exists on this install.")
    ok(r, 201)
    creator = ok(c.post("/creators", json={"name": "E2E creator"}), 201)
    grant = ok(c.post(f"/creators/{creator['id']}/rights-grants", json={"basis": "own_content"}), 201)
    project = ok(c.post("/projects", json={"name": "E2E", "creator_id": creator["id"]}), 201)

    def start(path: Path) -> dict[str, Any]:
        return ok(
            c.post(
                f"/projects/{project['id']}/uploads",
                json={
                    "filename": path.name,
                    "size_bytes": path.stat().st_size,
                    "rights_grant_id": grant["id"],
                },
            ),
            201,
        )

    # 1) VFR 1080p60 through the real workers
    print("1) VFR 1080p60, 2 min")
    vfr = work / "vfr_1080p60.mp4"
    make_vfr(vfr)
    up = start(vfr)
    for n in range(up["total_chunks"]):
        put(c, vfr, up, n)
    done = ok(c.post(f"/uploads/{up['upload_id']}/complete"))
    t0 = time.monotonic()
    m = wait_ready(c, done["media_id"], 900)
    stages = {s["stage"]: s for s in m["latest_run"]["stages"]}
    print(
        f"   -> {m['status']} in {time.monotonic() - t0:.0f}s; vfr={m['is_vfr']} "
        f"{m['width']}x{m['height']} {m['fps_num']}/{m['fps_den']} "
        f"mezzanine={stages['mezzanine']['metrics']} "
        f"proxy={stages['proxy']['metrics']}"
    )
    assert m["status"] == "ready" and m["is_vfr"] is True
    assert stages["mezzanine"]["metrics"]["transcoded"] is True
    tr = stages["transcribe"]["metrics"]
    print(
        f"   transcribe: {tr['device']} {tr['model']} {tr['words']} words, "
        f"{tr['realtime_factor']}x realtime, "
        f"VRAM peak {tr['gpu_asr']['used_peak_mib']} MiB, after {tr['gpu_asr']['used_after_mib']} MiB"
    )
    t = ok(c.get(f"/media/{done['media_id']}/transcript", params={"from_ms": 30000, "to_ms": 45000}))
    assert t["segments"] and all(s["end_ms"] > 30000 and s["start_ms"] < 45000 for s in t["segments"])
    print(f"   transcript 0:30-0:45: {' '.join(s['text'] for s in t['segments'])[:160]}")

    # 2) Big upload with interruptions
    big = work / "big.mp4"
    print(f"2) building ~{args.big_gb} GB video")
    make_big(big, args.big_gb, work)
    size = big.stat().st_size
    print(f"   {size / 1024**3:.2f} GB; hashing locally")
    local_sha = sha256(big)
    up = start(big)
    total = up["total_chunks"]
    print(f"   {total} chunks of {up['chunk_size'] // 1024**2} MB")

    t0 = time.monotonic()
    stop_at = int(total * 0.4)
    for n in range(stop_at):
        put(c, big, up, n)

    # (a) connection drops in the middle of a chunk: stream half a chunk, then fail
    def broken_body() -> Iterator[bytes]:
        data = chunk(big, up, stop_at)
        yield data[: len(data) // 2]
        raise ConnectionError("simulated network drop")

    try:
        c.put(
            f"/uploads/{up['upload_id']}/chunks/{stop_at}",
            content=broken_body(),
            headers={"Content-Length": str(len(chunk(big, up, stop_at)))},
        )
    except Exception as exc:
        print(f"   simulated drop mid-chunk {stop_at}: {type(exc).__name__}")
    # (b) the client process "dies": throw the session away, start a new client that knows only upload_id
    c.close()
    c = httpx.Client(base_url=BASE, timeout=120)
    ok(c.post("/auth/login", json={"email": "e2e@example.com", "password": password}))
    status = ok(c.get(f"/uploads/{up['upload_id']}"))
    have = set(status["received_chunks"])
    assert have == set(range(stop_at)), f"server state wrong after drop: {sorted(have)[-3:]}"
    print(f"   resume: server has {len(have)}/{total} chunks ({status['received_bytes'] / 1024**3:.2f} GB)")
    r = c.post(f"/uploads/{up['upload_id']}/complete")
    assert r.status_code == 409, "complete must refuse while chunks are missing"
    for n in range(total):
        if n not in have:
            put(c, big, up, n)
    elapsed = time.monotonic() - t0
    print(f"   uploaded in {elapsed:.0f}s ({size / 1024**2 / elapsed:.0f} MB/s)")
    done = ok(c.post(f"/uploads/{up['upload_id']}/complete"))
    t0 = time.monotonic()
    m = wait_ready(c, done["media_id"], 3600)
    print(
        f"   -> {m['status']} in {time.monotonic() - t0:.0f}s; {m['duration_ms'] / 1000:.0f}s "
        f"{m['width']}x{m['height']} {m['video_codec']}/{m['audio_codec']} vfr={m['is_vfr']}"
    )
    assert m["status"] == "ready", m["error"]
    assert m["sha256"] == local_sha, "reassembled file differs from the source"
    print("   sha256 matches the source file")

    # 3) Re-running analysis is a no-op
    run = ok(c.post(f"/media/{done['media_id']}/analyze"))
    assert run["status"] == "succeeded" and {s["status"] for s in run["stages"]} == {"skipped"}
    print("3) re-analyze: all stages skipped (no-op)")

    for p in (vfr, big):
        p.unlink(missing_ok=True)
    print("E2E PASSED")


if __name__ == "__main__":
    main()
