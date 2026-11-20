"""Builds evals/datasets/synthetic_stream_v1.json: a ~30 min scripted gaming-stream transcript with ordinary
filler and 8 planted, hand-written highlight moments at known times (the labels).

This is a *placeholder* eval set: it proves the plumbing and catches regressions (IDs, validation, boundaries,
ranking). The real baseline needs your own videos with moments you marked.

    python evals/make_synthetic_stream.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

WPS = 2.6  # words per second, typical streamer pace

FILLER = [
    "okay let me just grab this real quick", "alright chat what are we doing next", "I think the quest marker is over here",
    "hold on I need to check my inventory", "yeah I'm running low on ammo again", "thanks for the follow by the way",
    "let me turn the music down a little", "we should probably save the game first", "this area always takes forever",
    "I keep forgetting where the shop is", "okay heading north now", "someone asked about my settings I'll post them later",
    "let's see if this door opens", "nope that's locked", "I'm just going to loot everything here",
    "the map is kind of confusing in this part", "give me one second", "okay we're back", "selling the extra stuff now",
    "this is the boring part of the mission", "I need to upgrade my armor soon", "waiting for the elevator",
    "chat is saying go left so let's go left", "hmm that didn't work", "let me try the other path",
    "loading screen again", "drinking some water hold on", "okay where were we",
    "I'll do the side quest after this", "the graphics in this area are nice I guess",
]

# Each moment: list of (speaker, line). These are the "good clips" a human would pick.
MOMENTS = [
    ("story", [("Host", "okay so I never told you guys why I quit my old job"),
               ("Host", "my boss called me into his office and said we need to talk about your performance"),
               ("Host", "and I was so nervous I accidentally said I love you too instead of goodbye on the phone earlier that day"),
               ("Host", "and he goes I know that's what this meeting is about"),
               ("Host", "I handed in my notice the next morning"), ("Guest", "no way that actually happened")]),
    ("gaming", [("Host", "wait wait wait he's one hit he's one hit"), ("Host", "I have no health no ammo nothing"),
                ("Host", "if I throw the frying pan and it lands this is the greatest play of my life"),
                ("Host", "IT LANDED IT LANDED NO WAY"), ("Guest", "you just won a boss fight with a frying pan"),
                ("Host", "clip that clip that right now")]),
    ("opinion", [("Host", "unpopular opinion but tutorials ruin most games"),
                 ("Host", "the best games just drop you in and trust you to figure it out"),
                 ("Host", "when a game explains every button for two hours it's telling you it doesn't respect your time"),
                 ("Guest", "okay but how would anyone learn the combat"),
                 ("Host", "by dying that's literally how we all learned")]),
    ("funny", [("Guest", "why is your character walking backwards"),
               ("Host", "I swapped the controls to inverted as a joke for a viewer"),
               ("Host", "and now I can't find the setting to switch it back"),
               ("Host", "I've been playing backwards for forty minutes and I didn't say anything because I was embarrassed"),
               ("Guest", "you told us you were doing a challenge run")]),
    ("emotional", [("Host", "real talk for a second"),
                   ("Host", "a year ago I streamed to two people and one of them was my mom"),
                   ("Host", "today there are three thousand of you here"),
                   ("Host", "I don't know how to say thank you enough honestly"),
                   ("Host", "you guys changed my life")]),
    ("surprising", [("Host", "did you know the developers hid an entire second ending in this game"),
                    ("Host", "you have to refuse the final quest three times in a row"),
                    ("Host", "and then the main villain just sits down and asks you to tell him a story"),
                    ("Guest", "that cannot be real"), ("Host", "I'll prove it after this mission")]),
    ("debate", [("Guest", "pineapple belongs on pizza and I will die on this hill"),
                ("Host", "absolutely not fruit has no business near cheese"),
                ("Guest", "tomatoes are a fruit so you've been eating fruit pizza your whole life"),
                ("Host", "that is the most annoying correct thing anyone has ever said to me")]),
    ("reaction", [("Host", "what was that noise"), ("Host", "there is something in the vents"),
                  ("Host", "oh no oh no it's right behind me"), ("Host", "I'm not doing this I'm not doing this"),
                  ("Guest", "you screamed so loud my headset clipped")]),
]


def build(seed: int = 4, minutes: float = 30.0) -> dict[str, object]:
    rng = random.Random(seed)
    total_ms = int(minutes * 60_000)
    # place moments evenly with jitter, at least ~3 min apart
    slots = [int((i + 0.5) * total_ms / len(MOMENTS)) + rng.randint(-40_000, 40_000) for i in range(len(MOMENTS))]
    segments: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    t = 2_000
    mi = 0

    def add(speaker: str, text: str) -> None:
        nonlocal t
        words = text.split()
        dur = int(len(words) / WPS * 1000)
        step = dur / len(words)
        ws = [{"w": w, "s": int(t + k * step), "e": int(t + (k + 0.85) * step)} for k, w in enumerate(words)]
        segments.append({"idx": len(segments), "start_ms": t, "end_ms": t + dur, "speaker": speaker,
                         "text": text[0].upper() + text[1:] + ".", "words": ws})
        t += dur + rng.choice((250, 400, 600, 900, 1500, 2500))

    while t < total_ms:
        if mi < len(MOMENTS) and t >= slots[mi]:
            category, lines = MOMENTS[mi]
            start = t
            for speaker, line in lines:
                add(speaker, line)
            labels.append({"start_ms": start, "end_ms": segments[-1]["end_ms"], "category": category})
            mi += 1
        else:
            add("Host" if rng.random() < 0.85 else "Guest", rng.choice(FILLER))
    return {"name": "synthetic_stream_v1", "duration_ms": t, "creator_context": "Gaming streamer with a co-host",
            "segments": segments, "labels": labels, "peaks": [], "cuts": []}


if __name__ == "__main__":
    out = Path(__file__).parent / "datasets" / "synthetic_stream_v1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"{out}: {len(data['segments'])} segments, {len(data['labels'])} labeled moments, "  # type: ignore[arg-type]
          f"{data['duration_ms'] / 60000:.1f} min")  # type: ignore[operator]
