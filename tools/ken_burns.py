#!/usr/bin/env python3
"""Render a short Ken Burns clip from a still image with FFmpeg zoompan."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


WIDTH = 1080
HEIGHT = 1920
FPS = 30
DURATION = 5.0
BACKGROUND = "0x0A0A0F"
MOTION_TYPES = ("pan-right", "zoom-in", "zoom-out", "drift-up", "drift-down")


def ffmpeg_expr(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def clamp_expr(value: str, lower: str, upper: str) -> str:
    return f"min(max({value},{lower}),{upper})"


def progress_expr(frame_count: int) -> str:
    denominator = max(frame_count - 1, 1)
    return f"on/{denominator}"


def zoom_defaults(motion_type: str, zoom_start: float | None, zoom_end: float | None) -> tuple[float, float]:
    if zoom_start is not None and zoom_end is not None:
        return zoom_start, zoom_end
    if motion_type == "zoom-in":
        default_start, default_end = 1.0, 1.14
    elif motion_type == "zoom-out":
        default_start, default_end = 1.14, 1.0
    else:
        default_start, default_end = 1.08, 1.12
    return zoom_start if zoom_start is not None else default_start, zoom_end if zoom_end is not None else default_end


def build_position_expr(motion_type: str, axis: str, speed: float, progress: str) -> str:
    range_expr = f"i{axis}-i{axis}/zoom"
    centered = f"({range_expr})/2"
    distance = max(0.0, min(abs(speed), 0.95))

    if motion_type == "pan-right" and axis == "w":
        value = f"({range_expr})*({ffmpeg_expr(0.5 - distance / 2)}+{ffmpeg_expr(distance)}*{progress})"
    elif motion_type == "drift-up" and axis == "h":
        value = f"({range_expr})*({ffmpeg_expr(0.5 - distance / 2)}+{ffmpeg_expr(distance)}*{progress})"
    elif motion_type == "drift-down" and axis == "h":
        value = f"({range_expr})*({ffmpeg_expr(0.5 + distance / 2)}-{ffmpeg_expr(distance)}*{progress})"
    else:
        value = centered

    return clamp_expr(value, "0", range_expr)


def build_filter(args: argparse.Namespace) -> str:
    frame_count = round(args.duration * args.fps)
    zoom_start, zoom_end = zoom_defaults(args.motion_type, args.zoom_start, args.zoom_end)
    if zoom_start < 1.0 or zoom_end < 1.0:
        raise ValueError("zoom_start and zoom_end must be >= 1.0 for FFmpeg zoompan")
    if args.quality_scale < 1.0:
        raise ValueError("quality_scale must be >= 1.0")

    stage_width = int(args.width * args.quality_scale)
    stage_height = int(args.height * args.quality_scale)
    if stage_width % 2:
        stage_width += 1
    if stage_height % 2:
        stage_height += 1

    progress = progress_expr(frame_count)
    zoom_delta = zoom_end - zoom_start
    zoom = f"{ffmpeg_expr(zoom_start)}+({ffmpeg_expr(zoom_delta)})*{progress}"
    x_expr = build_position_expr(args.motion_type, "w", args.pan_x_speed, progress)
    y_expr = build_position_expr(args.motion_type, "h", args.pan_y_speed, progress)

    return ",".join(
        [
            f"scale={stage_width}:{stage_height}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={stage_width}:{stage_height}:(ow-iw)/2:(oh-ih)/2:color={BACKGROUND}",
            (
                "zoompan="
                f"z='{zoom}':"
                f"x='{x_expr}':"
                f"y='{y_expr}':"
                f"d={frame_count}:"
                f"s={args.width}x{args.height}:"
                f"fps={args.fps}"
            ),
            "setsar=1",
            "format=yuv420p",
        ]
    )


def render_clip(args: argparse.Namespace) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required but was not found on PATH")
    input_path = args.input_image.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    if args.output:
        output_path = args.output.expanduser().resolve()
    else:
        output_path = input_path.with_name(f"{input_path.stem}_{args.motion_type}.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = round(args.duration * args.fps)
    filter_graph = build_filter(args)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(args.fps),
        "-i",
        str(input_path),
        "-vf",
        filter_graph,
        "-frames:v",
        str(frame_count),
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        args.preset,
        "-crf",
        str(args.crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]
    subprocess.run(command, check=True)
    print(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a 9:16 Ken Burns MP4 from a still image using FFmpeg zoompan.")
    parser.add_argument("input_image", type=Path, help="Path to the still image to animate")
    parser.add_argument("--output", type=Path, help="Output MP4 path")
    parser.add_argument("--motion-type", choices=MOTION_TYPES, default="zoom-in")
    parser.add_argument("--duration", type=float, default=DURATION, help="Clip duration in seconds")
    parser.add_argument("--fps", type=int, default=FPS, help="Output frames per second")
    parser.add_argument("--width", type=int, default=WIDTH, help="Output width")
    parser.add_argument("--height", type=int, default=HEIGHT, help="Output height")
    parser.add_argument("--zoom-start", type=float, help="Starting zoom factor, must be >= 1.0")
    parser.add_argument("--zoom-end", type=float, help="Ending zoom factor, must be >= 1.0")
    parser.add_argument("--pan-x-speed", type=float, default=0.7, help="Horizontal crop-range fraction traversed over the clip")
    parser.add_argument("--pan-y-speed", type=float, default=0.7, help="Vertical crop-range fraction traversed over the clip")
    parser.add_argument("--quality-scale", type=float, default=2.0, help="Internal render scale before zoompan downsample")
    parser.add_argument("--crf", type=int, default=18, help="H.264 quality; lower is higher quality")
    parser.add_argument("--preset", default="medium", help="libx264 preset")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")
    return args


def main() -> None:
    render_clip(parse_args())


if __name__ == "__main__":
    main()
