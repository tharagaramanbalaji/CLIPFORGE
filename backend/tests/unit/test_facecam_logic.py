from __future__ import annotations

from clipforge.engine.render.facecam import FacecamResult, presence_intervals
from clipforge.engine.render.layouts import caption_margin_v, layout_filter
from clipforge.engine.render.spec import PRESETS, RenderSpec


def test_presence_intervals_smooth_gaps_and_drop_blips() -> None:
    on = [True] * 8 + [False] * 1 + [True] * 4 + [False] * 5 + [True] + [False] * 3  # 1 s samples
    samples = [(10_000 + i * 1000, ok) for i, ok in enumerate(on)]
    iv = presence_intervals(samples, 10_000, 32_000, 1000)
    assert iv == [[0, 12500]]  # 1 s gap bridged; the lone 1 s blip later is dropped (< 2 s)


def test_present_share_uses_nearest_sample_for_short_clips() -> None:
    r = FacecamResult([0.7, 0.5, 0.3, 0.5], [(0, True), (5000, False)], 5000)
    assert r.present_share(4200, 4800) == 0.0 and r.present_share(0, 5000) == 0.5


def test_switch_graph_and_caption_spot() -> None:
    spec = RenderSpec(
        start_ms=0,
        end_ms=20_000,
        frame_mode="auto_switch",
        extra={
            "layout": {
                "facecam": [0.745, 0.556, 0.255, 0.444],
                "facecam_intervals": [[500, 8500], [11500, 17400]],
                "src_aspect": 16 / 9,
                "captions": "below",
            }
        },
    )
    g = layout_filter(spec, PRESETS["shorts"], "[0:v]trim=start=0:end=20,", "setsar=1,format=yuv420p[v]")
    assert "split=3" in g and "enable='between(t,0.500,8.499)+between(t,11.500,17.399)'" in g
    mv = caption_margin_v(spec, PRESETS["shorts"], 100, 440)
    assert mv is not None and 1920 - mv - 100 >= 1264  # below the full picture, same spot in both layouts
