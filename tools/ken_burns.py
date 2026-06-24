#!/usr/bin/env python3
"""Render short 9:16 Ken Burns clips from still images with smooth subpixel motion."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WIDTH = 1080
HEIGHT = 1920
FPS = 30
DURATION = 5.0
BACKGROUND = "0x0A0A0F"
BACKGROUND_RGB = (10, 10, 15)
RANDOM_VARIATION = 0.08
BUFFER_SCALE = 1.2
MOTION_BLUR_PIXELS = 3
MOTION_BLUR_THRESHOLD = 0.08

LEGACY_MOTION_TYPES = ("pan-right", "zoom-in", "zoom-out", "drift-up", "drift-down")


@dataclass(frozen=True)
class MotionPreset:
    name: str
    zoom_start: float
    zoom_end: float
    x_start: float = 0.5
    x_end: float = 0.5
    y_start: float = 0.5
    y_end: float = 0.5
    zoom_mid: float | None = None
    description: str = ""


PRESETS: dict[str, MotionPreset] = {
    "vertical_zoom": MotionPreset(
        name="vertical_zoom",
        zoom_start=1.0,
        zoom_end=1.15,
        y_start=0.28,
        y_end=0.72,
        description="Slow upward drift plus gradual zoom in.",
    ),
    "horizontal_zoom": MotionPreset(
        name="horizontal_zoom",
        zoom_start=1.0,
        zoom_end=1.12,
        x_start=0.25,
        x_end=0.75,
        description="Rightward pan plus zoom in.",
    ),
    "unzoom_drift": MotionPreset(
        name="unzoom_drift",
        zoom_start=1.2,
        zoom_end=1.0,
        y_start=0.42,
        y_end=0.62,
        description="Start close, pull back, and drift upward subtly.",
    ),
    "push_diagonal": MotionPreset(
        name="push_diagonal",
        zoom_start=1.0,
        zoom_end=1.18,
        x_start=0.25,
        x_end=0.76,
        y_start=0.24,
        y_end=0.74,
        description="Diagonal pan up-right plus stronger push zoom.",
    ),
    "breathe": MotionPreset(
        name="breathe",
        zoom_start=1.0,
        zoom_mid=1.06,
        zoom_end=1.0,
        x_start=0.49,
        x_end=0.51,
        y_start=0.51,
        y_end=0.49,
        description="Gentle zoom in then out with near-static pan.",
    ),
    "drift_left_zoom": MotionPreset(
        name="drift_left_zoom",
        zoom_start=1.0,
        zoom_end=1.13,
        x_start=0.75,
        x_end=0.25,
        y_start=0.48,
        y_end=0.52,
        description="Leftward pan plus zoom in.",
    ),
    "slow_push": MotionPreset(
        name="slow_push",
        zoom_start=1.0,
        zoom_end=1.08,
        x_start=0.5,
        x_end=0.5,
        y_start=0.5,
        y_end=0.5,
        description="Very slow centered zoom in only.",
    ),
    "pullback_reveal": MotionPreset(
        name="pullback_reveal",
        zoom_start=1.15,
        zoom_end=1.0,
        x_start=0.46,
        x_end=0.62,
        y_start=0.5,
        y_end=0.5,
        description="Start tight on center, pull back, and drift right.",
    ),
}
PRESET_NAMES = tuple(PRESETS)

LEGACY_TO_PRESET = {
    "pan-right": "horizontal_zoom",
    "zoom-in": "slow_push",
    "zoom-out": "pullback_reveal",
    "drift-up": "vertical_zoom",
    "drift-down": "slow_push",
}


def ffmpeg_expr(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def ensure_deps() -> tuple[Any, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover - runtime environment check
        raise RuntimeError("smooth Ken Burns rendering requires numpy and opencv-python-headless") from exc
    return cv2, np


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def precompute_motion_curve(frame_count: int) -> list[float]:
    denominator = max(frame_count - 1, 1)
    return [smoothstep(index / denominator) for index in range(frame_count)]


def precompute_motion_values(start: float, end: float, curve: list[float]) -> list[float]:
    return [start + (end - start) * eased for eased in curve]


def precompute_zoom_values(preset: MotionPreset, frame_count: int) -> list[float]:
    if preset.zoom_mid is None:
        return precompute_motion_values(preset.zoom_start, preset.zoom_end, precompute_motion_curve(frame_count))

    values: list[float] = []
    denominator = max(frame_count - 1, 1)
    for index in range(frame_count):
        t = index / denominator
        if t <= 0.5:
            values.append(preset.zoom_start + (preset.zoom_mid - preset.zoom_start) * smoothstep(t * 2.0))
        else:
            values.append(preset.zoom_mid + (preset.zoom_end - preset.zoom_mid) * smoothstep((t - 0.5) * 2.0))
    return values


def buffer_size(width: int, height: int) -> tuple[int, int]:
    buffer_width = int(round(width * BUFFER_SCALE))
    buffer_height = int(round(height * BUFFER_SCALE))
    if buffer_width % 2:
        buffer_width += 1
    if buffer_height % 2:
        buffer_height += 1
    return buffer_width, buffer_height


def fit_cover_array(image: Any, width: int, height: int, cv2: Any, np: Any) -> Any:
    src_h, src_w = image.shape[:2]
    scale = max(width / src_w, height / src_h)
    resized_w = int(math.ceil(src_w * scale))
    resized_h = int(math.ceil(src_h * scale))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_LANCZOS4)
    left = max(0, (resized_w - width) // 2)
    top = max(0, (resized_h - height) // 2)
    return resized[top : top + height, left : left + width].astype("float32")


def render_camera_frame(source: Any, width: int, height: int, x_position: float, y_position: float, zoom: float, cv2: Any, np: Any) -> Any:
    buffer_height, buffer_width = source.shape[:2]
    half_crop_w = width / (2.0 * zoom)
    half_crop_h = height / (2.0 * zoom)
    min_center_x = half_crop_w
    max_center_x = buffer_width - half_crop_w
    min_center_y = half_crop_h
    max_center_y = buffer_height - half_crop_h
    center_x = min_center_x + (max_center_x - min_center_x) * max(0.0, min(1.0, x_position))
    center_y = min_center_y + (max_center_y - min_center_y) * max(0.0, min(1.0, y_position))
    matrix = np.float32(
        [
            [1.0 / zoom, 0.0, center_x - width / (2.0 * zoom)],
            [0.0, 1.0 / zoom, center_y - height / (2.0 * zoom)],
        ]
    )
    return cv2.warpAffine(
        source,
        matrix,
        (width, height),
        flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REFLECT,
        borderValue=BACKGROUND_RGB,
    )


def directional_motion_blur(frame: Any, dx: float, dy: float, cv2: Any, np: Any) -> Any:
    distance = math.hypot(dx, dy)
    if distance < MOTION_BLUR_THRESHOLD:
        return frame
    kernel = np.zeros((MOTION_BLUR_PIXELS, MOTION_BLUR_PIXELS), dtype="float32")
    center = (MOTION_BLUR_PIXELS - 1) / 2.0
    unit_x = dx / distance
    unit_y = dy / distance
    for offset in (-1.0, 0.0, 1.0):
        x = center + unit_x * offset
        y = center + unit_y * offset
        left = int(math.floor(x))
        top = int(math.floor(y))
        for yy in (top, top + 1):
            for xx in (left, left + 1):
                if 0 <= xx < MOTION_BLUR_PIXELS and 0 <= yy < MOTION_BLUR_PIXELS:
                    kernel[yy, xx] += max(0.0, 1.0 - abs(x - xx)) * max(0.0, 1.0 - abs(y - yy))
    total = float(kernel.sum())
    if total <= 0.0:
        return frame
    kernel /= total
    return cv2.filter2D(frame, -1, kernel)


def clamp_expr(value: str, lower: str, upper: str) -> str:
    return f"min(max({value},{lower}),{upper})"


def progress_expr(frame_count: int) -> str:
    denominator = max(frame_count - 1, 1)
    return f"on/{denominator}"


def filename_seed(path: Path, suffix: str = "") -> int:
    digest = hashlib.sha256(f"{path.name}:{suffix}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def resolve_preset(args: argparse.Namespace, input_path: Path) -> MotionPreset:
    preset_name = args.motion_preset
    if args.motion_type:
        preset_name = LEGACY_TO_PRESET[args.motion_type]
    if preset_name == "random":
        preset_name = random.Random(filename_seed(input_path, "preset")).choice(PRESET_NAMES)
    base = PRESETS[preset_name]

    zoom_start = args.zoom_start if args.zoom_start is not None else base.zoom_start
    zoom_end = args.zoom_end if args.zoom_end is not None else base.zoom_end
    if args.randomize:
        rng = random.Random(filename_seed(input_path, preset_name))
        zoom_delta = (zoom_end - zoom_start) * rng.uniform(1 - RANDOM_VARIATION, 1 + RANDOM_VARIATION)
        zoom_end = max(1.0, zoom_start + zoom_delta)
        x_start, x_end = vary_pan(base.x_start, base.x_end, rng)
        y_start, y_end = vary_pan(base.y_start, base.y_end, rng)
        zoom_mid = None
        if base.zoom_mid is not None:
            mid_delta = (base.zoom_mid - zoom_start) * rng.uniform(1 - RANDOM_VARIATION, 1 + RANDOM_VARIATION)
            zoom_mid = max(1.0, zoom_start + mid_delta)
    else:
        x_start, x_end = base.x_start, base.x_end
        y_start, y_end = base.y_start, base.y_end
        zoom_mid = base.zoom_mid

    x_start = args.x_start if args.x_start is not None else x_start
    x_end = args.x_end if args.x_end is not None else x_end
    y_start = args.y_start if args.y_start is not None else y_start
    y_end = args.y_end if args.y_end is not None else y_end

    return MotionPreset(
        name=preset_name,
        zoom_start=zoom_start,
        zoom_mid=zoom_mid,
        zoom_end=zoom_end,
        x_start=x_start,
        x_end=x_end,
        y_start=y_start,
        y_end=y_end,
        description=base.description,
    )


def vary_pan(start: float, end: float, rng: random.Random) -> tuple[float, float]:
    midpoint = (start + end) / 2
    travel = (end - start) * rng.uniform(1 - RANDOM_VARIATION, 1 + RANDOM_VARIATION)
    offset = rng.uniform(-RANDOM_VARIATION, RANDOM_VARIATION)
    new_start = midpoint - travel / 2 + offset
    new_end = midpoint + travel / 2 + offset
    return min(max(new_start, 0.05), 0.95), min(max(new_end, 0.05), 0.95)


def zoom_expression(preset: MotionPreset, progress: str) -> str:
    zoom_start = ffmpeg_expr(preset.zoom_start)
    zoom_end = ffmpeg_expr(preset.zoom_end)
    if preset.zoom_mid is None:
        zoom_delta = ffmpeg_expr(preset.zoom_end - preset.zoom_start)
        return f"{zoom_start}+({zoom_delta})*{progress}"
    zoom_mid = ffmpeg_expr(preset.zoom_mid)
    first_delta = ffmpeg_expr((preset.zoom_mid - preset.zoom_start) * 2)
    second_delta = ffmpeg_expr((preset.zoom_end - preset.zoom_mid) * 2)
    return f"if(lte({progress},0.5),{zoom_start}+({first_delta})*{progress},{zoom_mid}+({second_delta})*({progress}-0.5))"


def position_expr(axis: str, start: float, end: float, progress: str) -> str:
    range_expr = f"i{axis}-i{axis}/zoom"
    if abs(start - end) < 0.00001:
        value = f"({range_expr})*{ffmpeg_expr(start)}"
    else:
        value = f"({range_expr})*({ffmpeg_expr(start)}+({ffmpeg_expr(end - start)})*{progress})"
    return clamp_expr(value, "0", range_expr)


def stage_size(width: int, height: int, quality_scale: float) -> tuple[int, int]:
    if quality_scale < 2.0:
        raise ValueError("quality_scale must be >= 2.0 so input is loaded at least 2x output resolution")
    stage_width = int(width * quality_scale)
    stage_height = int(height * quality_scale)
    if stage_width % 2:
        stage_width += 1
    if stage_height % 2:
        stage_height += 1
    return stage_width, stage_height


def build_filter(args: argparse.Namespace, preset: MotionPreset) -> str:
    frame_count = round(args.duration * args.fps)
    if preset.zoom_start < 1.0 or preset.zoom_end < 1.0 or (preset.zoom_mid is not None and preset.zoom_mid < 1.0):
        raise ValueError("zoom values must be >= 1.0 for FFmpeg zoompan")

    render_width, render_height = stage_size(args.width, args.height, args.quality_scale)
    progress = progress_expr(frame_count)
    zoom = zoom_expression(preset, progress)
    x_expr = position_expr("w", preset.x_start, preset.x_end, progress)
    y_expr = position_expr("h", preset.y_start, preset.y_end, progress)

    return ",".join(
        [
            f"scale={render_width}:{render_height}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={render_width}:{render_height}:(ow-iw)/2:(oh-ih)/2:color={BACKGROUND}",
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


def render_clip(args: argparse.Namespace) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required but was not found on PATH")
    cv2, np = ensure_deps()
    input_path = args.input_image.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    preset = resolve_preset(args, input_path)
    output_path = args.output.expanduser().resolve() if args.output else input_path.with_name(f"{input_path.stem}_{preset.name}.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = round(args.duration * args.fps)

    source_bgr = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if source_bgr is None:
        raise RuntimeError(f"could not read image: {input_path}")
    source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)
    buffer_width, buffer_height = buffer_size(args.width, args.height)
    source = fit_cover_array(source_rgb, buffer_width, buffer_height, cv2, np)

    motion_curve = precompute_motion_curve(frame_count)
    x_values = precompute_motion_values(preset.x_start, preset.x_end, motion_curve)
    y_values = precompute_motion_values(preset.y_start, preset.y_end, motion_curve)
    zoom_values = precompute_zoom_values(preset, frame_count)

    with tempfile.TemporaryDirectory(prefix="ken_burns_frames_") as temp_name:
        frame_dir = Path(temp_name)
        previous_x = x_values[0]
        previous_y = y_values[0]
        for index, (x_position, y_position, zoom) in enumerate(zip(x_values, y_values, zoom_values)):
            frame = render_camera_frame(source, args.width, args.height, x_position, y_position, zoom, cv2, np)
            frame = directional_motion_blur(frame, x_position - previous_x, y_position - previous_y, cv2, np)
            previous_x = x_position
            previous_y = y_position
            frame_u8 = np.clip(frame, 0, 255).astype("uint8")
            cv2.imwrite(str(frame_dir / f"frame_{index:05d}.png"), cv2.cvtColor(frame_u8, cv2.COLOR_RGB2BGR))

        command = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-framerate",
            str(args.fps),
            "-i",
            str(frame_dir / "frame_%05d.png"),
            "-c:v",
            "libx264",
            "-preset",
            args.encoder_preset,
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

    output_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "renderer": "opencv_smooth_subpixel",
                "preset": preset.name,
                "frames": frame_count,
                "fps": args.fps,
                "size": [args.width, args.height],
                "buffer_size": [buffer_width, buffer_height],
                "easing": "smoothstep",
                "interpolation": "cv2.INTER_LANCZOS4",
                "motion_blur_pixels": MOTION_BLUR_PIXELS,
            },
            indent=2,
        )
        + "\n"
    )
    print(output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a 9:16 Ken Burns MP4 from a still image using FFmpeg zoompan.")
    parser.add_argument("input_image", type=Path, help="Path to the still image to animate")
    parser.add_argument("--output", type=Path, help="Output MP4 path")
    parser.add_argument("--preset", dest="motion_preset", choices=PRESET_NAMES + ("random",), default="slow_push", help="Organic motion preset")
    parser.add_argument("--motion-type", choices=LEGACY_MOTION_TYPES, help="Deprecated v1 motion alias; maps to a v2 preset")
    parser.add_argument("--randomize", action="store_true", help="Apply deterministic ±8%% variation seeded by image filename")
    parser.add_argument("--duration", type=float, default=DURATION, help="Clip duration in seconds")
    parser.add_argument("--fps", type=int, default=FPS, help="Output frames per second")
    parser.add_argument("--width", type=int, default=WIDTH, help="Output width")
    parser.add_argument("--height", type=int, default=HEIGHT, help="Output height")
    parser.add_argument("--zoom-start", type=float, help="Override starting zoom factor, must be >= 1.0")
    parser.add_argument("--zoom-end", type=float, help="Override ending zoom factor, must be >= 1.0")
    parser.add_argument("--x-start", type=float, help="Override normalized starting x pan position")
    parser.add_argument("--x-end", type=float, help="Override normalized ending x pan position")
    parser.add_argument("--y-start", type=float, help="Override normalized starting y pan position")
    parser.add_argument("--y-end", type=float, help="Override normalized ending y pan position")
    parser.add_argument("--quality-scale", type=float, default=2.0, help="Internal render scale; must be >= 2.0")
    parser.add_argument("--crf", type=int, default=18, help="H.264 quality; lower is higher quality")
    parser.add_argument("--encoder-preset", default="medium", help="libx264 encoder preset")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")
    if args.quality_scale < 2.0:
        parser.error("--quality-scale must be at least 2.0")
    return args


def main() -> None:
    render_clip(parse_args())


if __name__ == "__main__":
    main()
