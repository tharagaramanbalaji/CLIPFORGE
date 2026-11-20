from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

from clipforge.engine.export.premiere import Moment, Source, build_project_xml, pathurl, rate_xml


def test_project_xml_structure_and_frames() -> None:
    src = Source(
        "I Think I’m Done.mp4",  # noqa: RUF001 - curly apostrophe on purpose (real filename, tests encoding)
        460_846,
        1280,
        720,
        Fraction(30),
        local_path=r"C:\Users\me\Downloads\I Think I’m Done.mp4",  # noqa: RUF001
    )
    xml = build_project_xml(
        "ClipForge test",
        src,
        [Moment(92_000, 110_000, "#1 Loud moment", "score 0.6"), Moment(312_900, 329_500, "#2 Goal")],
        [Moment(312_900, 329_500, "Goal & reaction")],
    )
    root = ET.fromstring(xml.split("\n", 2)[2])  # skip XML declaration + DOCTYPE
    seqs = root.findall("./project/children/sequence")
    assert [s.findtext("name") for s in seqs] == ["All highlights", "01 Goal & reaction (9x16)"]
    allh = seqs[0]
    items = allh.findall("./media/video/track/clipitem")
    assert [
        (int(i.findtext("start")), int(i.findtext("end")), int(i.findtext("in")), int(i.findtext("out")))
        for i in items
    ] == [(0, 540, 2760, 3300), (540, 1038, 9387, 9885)]  # back-to-back on the timeline
    assert int(allh.findtext("duration")) == 1038
    assert [m.findtext("name") for m in allh.findall("marker")] == ["#1 Loud moment", "#2 Goal"]
    assert len(allh.findall("./media/audio/track/clipitem")) == 2
    # the media file is defined once and referenced afterwards
    files = root.iter("file")
    defined = [f for f in files if f.find("pathurl") is not None]
    assert len(defined) == 1
    assert (
        defined[0].findtext("pathurl")
        == "file://localhost/C:/Users/me/Downloads/I%20Think%20I%E2%80%99m%20Done.mp4"
    )
    vert = seqs[1]
    fmt = vert.find("./media/video/format/samplecharacteristics")
    assert (fmt.findtext("width"), fmt.findtext("height")) == ("1080", "1920")
    assert vert.find(".//parameter/value").text == "266.67"  # 1920 / 720 -> fills the vertical frame


def test_rates_and_relative_paths() -> None:
    assert "<timebase>30</timebase><ntsc>TRUE</ntsc>" in rate_xml(Fraction(30000, 1001))
    assert "<timebase>60</timebase><ntsc>FALSE</ntsc>" in rate_xml(Fraction(60))
    assert pathurl(None, "my vod.mp4") == "my%20vod.mp4"  # Premiere asks to locate it
