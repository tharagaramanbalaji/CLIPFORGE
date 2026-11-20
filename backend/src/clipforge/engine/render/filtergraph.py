"""FFmpeg argument/filtergraph builder for clip renders. Pure (unit-tested)."""

from __future__ import annotations

from pathlib import Path

from clipforge.engine.render.spec import Preset, RenderSpec

PRESEEK_S = 3.0  # input-seek this far before the clip, then cut exactly with trim


def preseek_s(spec: RenderSpec) -> float:
    return max(0.0, spec.start_s - PRESEEK_S)


def trim_filter(spec: RenderSpec, src_fps: float) -> str:
    """Select frames by timestamp with half-frame margins: exact regardless of where the seek landed
    (input seeking leaves the first timestamp a hair off, which made the fps filter drop/duplicate frames)."""
    half = 0.5 / src_fps
    start = spec.start_s - preseek_s(spec)
    end = start + spec.duration_s - half
    return f"trim=start={max(0.0, start - half):.6f}:end={end:.6f},setpts=PTS-STARTPTS"


def subtitles_filter(ass_path: Path | None) -> str:
    """libass with the bundled fonts only (fontsdir), so output doesn't depend on system fonts."""
    if ass_path is None:
        return ""
    from clipforge.engine.subtitles.styles import FONTS_DIR

    return f"subtitles=filename='{ass_path}':fontsdir='{FONTS_DIR}',"


def video_filter(
    spec: RenderSpec, preset: Preset, src_fps: float | None = None, ass_path: Path | None = None
) -> str:
    w, h = preset.width, preset.height
    subs = subtitles_filter(ass_path)
    src = src_fps or float(spec.fps)
    head = f"[0:v]{trim_filter(spec, src)},"
    fps = "" if abs(src - float(spec.fps)) < 1e-3 else f"fps={spec.fps_num}/{spec.fps_den},"
    if spec.frame_mode in ("facecam_split", "two_stack", "auto_switch"):
        from clipforge.engine.render.layouts import layout_filter

        return layout_filter(spec, preset, head, f"setsar=1,{fps}{subs}format=yuv420p[v]")
    if spec.frame_mode == "crop":
        return (
            f"{head}scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,"
            f"{fps}{subs}format=yuv420p[v]"
        )
    # blur_pad: the whole frame, centered, over a blurred zoomed copy (no black bars, nothing cropped)
    return (
        f"{head}split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
        f"boxblur=10:2,scale={w}:{h}[bg];"
        f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{fps}{subs}format=yuv420p[v]"
    )


def loudnorm_filter(preset: Preset, measured: dict[str, float] | None) -> str:
    base = f"loudnorm=I={preset.loudness_lufs}:TP={preset.true_peak_db}:LRA=11"
    if measured:  # pass 2: linear normalization using pass-1 measurements (no pumping)
        base += (
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:linear=true"
        )
    return base


def audio_filter(spec: RenderSpec, preset: Preset, has_audio: bool) -> str:
    dur = f"{spec.duration_s:.6f}"
    if not has_audio:  # always ship a (silent) audio track: some platforms reject video-only uploads
        return f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a]"
    start = spec.start_s - preseek_s(spec)
    return (
        f"[0:a]atrim=start={start:.6f},asetpts=PTS-STARTPTS,{loudnorm_filter(preset, spec.loudness)},"
        f"aresample=48000,apad,atrim=duration={dur},asetpts=PTS-STARTPTS[a]"
    )


def video_codec_args(spec: RenderSpec, preset: Preset) -> list[str]:
    gop = str(max(1, round(float(spec.fps) * 2)))
    vbv = ["-maxrate", preset.video_bitrate_cap, "-bufsize", preset.video_bitrate_cap]
    if spec.encoder == "nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "vbr",
            "-cq",
            str(spec.quality),
            "-b:v",
            "0",
            *vbv,
            "-profile:v",
            "high",
            "-g",
            gop,
            "-bf",
            "2",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(spec.quality),
        *vbv,
        "-profile:v",
        "high",
        "-g",
        gop,
        "-threads",
        "0",
    ]


def build_render_args(
    src: Path,
    dst: Path,
    spec: RenderSpec,
    preset: Preset,
    has_audio: bool,
    src_fps: float | None = None,
    ass_path: Path | None = None,
) -> list[str]:
    """Fast input seek to a few seconds before the clip, then frame-exact `trim` by timestamp.
    -frames:v pins the exact frame count."""
    graph = f"{video_filter(spec, preset, src_fps, ass_path)};{audio_filter(spec, preset, has_audio)}"
    return [
        "-y",
        "-ss",
        f"{preseek_s(spec):.6f}",
        "-i",
        str(src),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-frames:v",
        str(spec.frame_count),
        *video_codec_args(spec, preset),
        "-c:a",
        "aac",
        "-b:a",
        preset.audio_bitrate,
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(dst),
    ]


def build_loudness_measure_args(src: Path, spec: RenderSpec, preset: Preset) -> list[str]:
    return [
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{spec.start_s:.6f}",
        "-t",
        f"{spec.duration_s:.6f}",
        "-i",
        str(src),
        "-vn",
        "-af",
        loudnorm_filter(preset, None) + ":print_format=json",
        "-f",
        "null",
        "-",
    ]


def build_thumbnail_args(src: Path, dst: Path, spec: RenderSpec, preset: Preset, at_s: float) -> list[str]:
    still = RenderSpec(
        start_ms=0,
        end_ms=10_000,
        preset=spec.preset,
        frame_mode=spec.frame_mode,
        fps_num=spec.fps_num,
        fps_den=spec.fps_den,
        extra={"layout": spec.extra.get("layout") or {}},  # same framing, no captions on the thumbnail
    )
    vf = video_filter(still, preset).replace("[v]", "")  # unlabelled output; [0:v] input stays explicit
    return [
        "-y",
        "-ss",
        f"{at_s:.3f}",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-filter_complex",
        vf,
        "-q:v",
        "3",
        str(dst),
    ]
