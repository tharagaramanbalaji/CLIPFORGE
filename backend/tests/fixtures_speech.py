"""Synthetic speech with exactly known word timings (espeak-ng + FFmpeg), for ASR accuracy tests.

Each word is synthesized on its own, trimmed to its audible part, and concatenated with short random
gaps (0-120 ms, like fast natural speech) and longer pauses between sentences. Ground truth = where each
word's audio was placed. Robotic voice, so this measures *alignment* accuracy, not accent robustness.
"""

from __future__ import annotations

import random
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

SR = 16_000

SENTENCES = [
    "we found the hidden door behind the old library",
    "nobody expected the final round to end like that",
    "she said the match was over before it even started",
    "turn left at the bridge and follow the river north",
    "this is the best moment of the entire stream",
    "they missed the last shot by less than a second",
    "please remember to check the settings before you play",
    "the crowd went wild when the team took the lead",
]

# Held out: never used to calibrate the start-lead correction; the accuracy test must pass on it too.
HELDOUT_SENTENCES = [
    "my brother bought a green bicycle at the market yesterday",
    "if the server crashes again we will restart from the beginning",
    "keep your shield up and wait for the perfect moment to strike",
    "seven players joined the lobby just before midnight",
    "the coffee was cold but the conversation was great",
    "honestly that might be the funniest thing that happened all week",
]

PARAGRAPHS = [
    "Welcome back to the stream everyone. Today we are trying the hardest level in the game, and honestly "
    "I have no idea if we can finish it. Last time we got close, but the boss at the end destroyed us.",
    "Let me tell you a quick story. When I started playing this game, I thought the map was small. Then "
    "I found a cave under the mountain, and it went on for hours. That was the moment I fell in love with it.",
    "Okay chat, we need a plan. If we go through the city, we lose time but we find better gear. If we go "
    "through the forest, we save time but we risk running into the big monster. What do you think?",
    "That was unbelievable. Did you see that jump? I did not think that would work at all. Somebody clip "
    "that right now, because nobody is going to believe me when I tell them about it later.",
]


@dataclass(frozen=True)
class TimedWord:
    word: str
    start_ms: int
    end_ms: int
    speaker: str = "A"


# Two synthetic voices for the diarization fixture: (espeak voice, pitch 0-99, words/min, timbre filter).
# Pitch alone is not enough: speaker-embedding models key on vocal-tract (formant) characteristics, and two
# espeak voices share one synthetic vocal tract, so pyannote heard a single speaker. The asetrate trick
# scales all formants (A: longer/deeper tract, B: shorter tract), like two physically different people.
VOICES = {
    "A": ("en-us", "35", "160", "asetrate=22050*0.85,aresample=22050"),
    "B": ("en-gb+f3", "70", "175", "asetrate=22050*1.3,aresample=22050"),
}

DIALOGUE = [
    ("A", "so what happened at the end of the match yesterday"),
    ("B", "honestly we were losing the whole time until the last minute"),
    ("A", "and then you just turned it around"),
    ("B", "yes we found the hidden path behind the castle and surprised them"),
    ("A", "that sounds incredible did anyone record it"),
    ("B", "the whole thing is on the stream you should watch it"),
    ("A", "I will tonight what was the hardest part"),
    ("B", "probably the boss fight because the timing was really strict"),
    ("A", "people always say that about this game"),
    ("B", "they are right but it feels amazing when you finally win"),
    ("A", "okay let us try it again right now"),
    ("B", "sure but this time you lead the team"),
]


def _run(*args: str) -> None:
    subprocess.run(list(args), check=True, timeout=120, capture_output=True)


def _pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.readframes(w.getnframes())


def _write(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * (SR * ms // 1000)


def _synth(
    text: str,
    work: Path,
    name: str,
    trim: bool,
    voice: tuple[str, str, str, str] = ("en-us", "50", "165", ""),
) -> bytes:
    raw = work / f"{name}_raw.wav"
    out = work / f"{name}.wav"
    _run("espeak-ng", "-v", voice[0], "-p", voice[1], "-s", voice[2], "-w", str(raw), text)
    timbre = f"{voice[3]}," if voice[3] else ""
    af = f"{timbre}aresample=16000"
    if trim:
        trim_f = "silenceremove=start_periods=1:start_threshold=-45dB:detection=peak"
        af = f"{timbre}{trim_f},areverse,{trim_f},areverse,aresample=16000"
    _run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(raw),
        "-ac",
        "1",
        "-af",
        af,
        "-c:a",
        "pcm_s16le",
        str(out),
    )
    return _pcm(out)


def make_timed_speech(
    out: Path, sentences: list[str] | None = None, seed: int = 7
) -> tuple[Path, list[TimedWord]]:
    """Returns (wav path, ground truth). ~25 s for the default sentences."""
    work = out / "words"
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    pcm = bytearray(_silence(600))
    truth: list[TimedWord] = []
    cache: dict[str, bytes] = {}
    for si, sentence in enumerate(sentences or SENTENCES):
        for word in sentence.split():
            if word not in cache:
                cache[word] = _synth(word, work, f"w{len(cache)}", trim=True)
            clip = cache[word]
            start = len(pcm) // 2 * 1000 // SR
            pcm += clip
            truth.append(TimedWord(word, start, start + len(clip) // 2 * 1000 // SR))
            pcm += _silence(rng.choice((0, 30, 60, 90, 120)))
        pcm += _silence(450 + 50 * (si % 3))
    path = out / "timed_speech.wav"
    _write(path, bytes(pcm))
    return path, truth


def make_dialogue(out: Path, seed: int = 3) -> tuple[Path, list[TimedWord]]:
    """Two speakers taking turns (~60 s). Ground truth has each word's speaker and timing."""
    work = out / "dialogue_words"
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    pcm = bytearray(_silence(500))
    truth: list[TimedWord] = []
    cache: dict[tuple[str, str], bytes] = {}
    for speaker, sentence in DIALOGUE:
        for word in sentence.split():
            if (speaker, word) not in cache:
                cache[(speaker, word)] = _synth(
                    word, work, f"{speaker}{len(cache)}", trim=True, voice=VOICES[speaker]
                )
            clip = cache[(speaker, word)]
            start = len(pcm) // 2 * 1000 // SR
            pcm += clip
            truth.append(TimedWord(word, start, start + len(clip) // 2 * 1000 // SR, speaker))
            pcm += _silence(rng.choice((0, 40, 80, 120)))
        pcm += _silence(rng.choice((250, 400, 600)))  # turn-taking gap
    path = out / "dialogue.wav"
    _write(path, bytes(pcm))
    return path, truth


def make_long_speech(out: Path, minutes: float) -> Path:
    """Natural-ish sentences (whole paragraphs synthesized) repeated with pauses up to `minutes`."""
    out.mkdir(parents=True, exist_ok=True)
    clips = [_synth(p, out, f"p{i}", trim=False) for i, p in enumerate(PARAGRAPHS)]
    target = int(minutes * 60 * SR * 2)
    path = out / f"long_{minutes:g}min.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        written, i = 0, 0
        while written < target:
            chunk = clips[i % len(clips)] + _silence(800 + 400 * (i % 4))
            chunk = chunk[: target - written]
            w.writeframes(chunk)
            written += len(chunk)
            i += 1
    return path
