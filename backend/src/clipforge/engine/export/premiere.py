"""Final Cut Pro 7 XML (xmeml v4): Premiere Pro imports it natively (File > Import), as do DaVinci Resolve and
Final Cut Pro. One project file with:
- "All highlights": source-resolution sequence, every moment cut back-to-back, a marker per moment;
- one vertical sequence per clip (scaled to fill 1080x1920, center), ready for captions/framing in Premiere.
Media is referenced by the owner's local path (pathurl); without it Premiere asks to relink (Locate) once.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from xml.sax.saxutils import escape


@dataclass(frozen=True, slots=True)
class Source:
    filename: str
    duration_ms: int
    width: int
    height: int
    fps: Fraction
    local_path: str | None = None  # e.g. C:\Users\me\Videos\stream.mp4 (where the file is on the editing PC)


@dataclass(frozen=True, slots=True)
class Moment:
    start_ms: int
    end_ms: int
    title: str
    note: str = ""


@dataclass
class _Ids:
    n: int = 0
    seen_file: set[str] = field(default_factory=set)

    def next(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}-{self.n}"


def pathurl(local_path: str | None, filename: str) -> str:
    if not local_path:
        return urllib.parse.quote(filename)  # relative: Premiere will offer to locate it
    p = local_path.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":  # Windows drive path
        return "file://localhost/" + p[0] + ":" + urllib.parse.quote(p[2:])
    return "file://localhost" + urllib.parse.quote(p if p.startswith("/") else "/" + p)


def rate_xml(fps: Fraction) -> str:
    timebase = round(float(fps))
    ntsc = abs(float(fps) - timebase * 1000 / 1001) < 0.01 and timebase != float(fps)
    return f"<rate><timebase>{timebase}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"


def frames(ms: int, fps: Fraction) -> int:
    return round(ms * float(fps) / 1000)


def _file_xml(src: Source, ids: _Ids) -> str:
    fid = "file-1"
    if fid in ids.seen_file:
        return f'<file id="{fid}"/>'
    ids.seen_file.add(fid)
    return (
        f'<file id="{fid}"><name>{escape(src.filename)}</name>'
        f"<pathurl>{escape(pathurl(src.local_path, src.filename))}"
        f"</pathurl>{rate_xml(src.fps)}<duration>{frames(src.duration_ms, src.fps)}</duration>"
        f"<media><video><samplecharacteristics>{rate_xml(src.fps)}<width>{src.width}</width>"
        f"<height>{src.height}</height></samplecharacteristics></video>"
        "<audio><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics>"
        "<channelcount>2</channelcount></audio></media></file>"
    )


def _scale_filter(scale_pct: float) -> str:
    return (
        "<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion"
        "</effectcategory><effecttype>motion</effecttype><mediatype>video</mediatype><parameter>"
        f"<parameterid>scale</parameterid><name>Scale</name><valuemin>0</valuemin><valuemax>1000</valuemax>"
        f"<value>{scale_pct:.2f}</value></parameter></effect></filter>"
    )


def _sequence(
    name: str,
    src: Source,
    moments: Sequence[Moment],
    width: int,
    height: int,
    ids: _Ids,
    scale_pct: float | None,
    markers: bool,
) -> str:
    fps, total = src.fps, frames(src.duration_ms, src.fps)
    vitems, aitems, marks = [], [], []
    pos = 0
    for m in moments:
        src_in, src_out = frames(m.start_ms, fps), frames(m.end_ms, fps)
        length = src_out - src_in
        common = (
            f"<name>{escape(m.title)}</name><duration>{total}</duration>{rate_xml(fps)}"
            f"<start>{pos}</start><end>{pos + length}</end><in>{src_in}</in><out>{src_out}</out>"
        )
        vitems.append(
            f'<clipitem id="{ids.next("clipitem")}">{common}{_file_xml(src, ids)}'
            f"{_scale_filter(scale_pct) if scale_pct else ''}</clipitem>"
        )
        aitems.append(
            f'<clipitem id="{ids.next("clipitem")}">{common}{_file_xml(src, ids)}'
            "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack></clipitem>"
        )
        if markers:
            marks.append(
                f"<marker><name>{escape(m.title)}</name><comment>{escape(m.note)}</comment>"
                f"<in>{pos}</in><out>-1</out></marker>"
            )
        pos += length
    fmt = (
        f"<format><samplecharacteristics>{rate_xml(fps)}<width>{width}</width><height>{height}</height>"
        "<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>"
        "</samplecharacteristics></format>"
    )
    return (
        f'<sequence id="{ids.next("sequence")}"><name>{escape(name)}</name><duration>{pos}</duration>'
        f"{rate_xml(fps)}<media><video>{fmt}<track>{''.join(vitems)}</track></video>"
        f"<audio><track>{''.join(aitems)}</track></audio></media>{''.join(marks)}</sequence>"
    )


def build_project_xml(
    project_name: str,
    src: Source,
    highlights: Sequence[Moment],
    vertical_clips: Sequence[Moment],
    vertical: tuple[int, int] = (1080, 1920),
) -> str:
    ids = _Ids()
    seqs = []
    if highlights:
        seqs.append(
            _sequence("All highlights", src, highlights, src.width, src.height, ids, None, markers=True)
        )
    fill = vertical[1] / src.height * 100  # fill the 9:16 frame's height; center of the picture is kept
    for i, m in enumerate(vertical_clips, 1):
        seqs.append(
            _sequence(
                f"{i:02d} {m.title} (9x16)", src, [m], vertical[0], vertical[1], ids, fill, markers=False
            )
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4"><project>'
        f"<name>{escape(project_name)}</name><children>{''.join(seqs)}</children></project></xmeml>\n"
    )
