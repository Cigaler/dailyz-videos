#!/usr/bin/env python3
"""Render a parallax animation from one still image.

The script first tries DepthFlow when installed. If DepthFlow is unavailable or
fails in the local environment, it falls back to a deterministic CPU-only manual
parallax renderer that can use MiDaS/transformers for depth when available and a
luminance/saliency depth estimate otherwise.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


WIDTH = 1080
HEIGHT = 1920
FPS = 30
DURATION = 5.0
BACKGROUND_RGB = (10, 10, 15)
BUFFER_SCALE = 1.2
MOTION_BLUR_PIXELS = 3
MOTION_BLUR_THRESHOLD = 0.08
LAYER_MULTIPLIERS = {
    "background": 0.7,
    "midground": 1.0,
    "foreground": 1.4,
}


def ensure_deps() -> tuple[Any, Any, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - runtime environment check
        raise RuntimeError("manual parallax fallback requires pillow, numpy, and opencv-python-headless") from exc
    return cv2, np, Image


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def precompute_motion_curve(frame_count: int) -> list[float]:
    denominator = max(frame_count - 1, 1)
    return [smoothstep(index / denominator) for index in range(frame_count)]


def precompute_motion_values(start: float, end: float, curve: list[float]) -> list[float]:
    return [start + (end - start) * eased for eased in curve]


def buffer_size(width: int, height: int) -> tuple[int, int]:
    buffer_width = int(round(width * BUFFER_SCALE))
    buffer_height = int(round(height * BUFFER_SCALE))
    if buffer_width % 2:
        buffer_width += 1
    if buffer_height % 2:
        buffer_height += 1
    return buffer_width, buffer_height


def run_depthflow(args: argparse.Namespace) -> bool:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(args.input_image),
        "--output",
        str(args.output),
        "--duration",
        str(args.duration),
        "--fps",
        str(args.fps),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--depth-scale",
        str(args.depth_scale),
        "--motion-speed",
        str(args.motion_speed),
        "--crf",
        str(args.crf),
        "--encoder-preset",
        args.encoder_preset,
        "--_depthflow-child",
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        print(f"DepthFlow render failed with exit {result.returncode}, using manual fallback.")
        if result.stdout:
            print(result.stdout[-4000:])
        return False
    if result.stdout:
        print(result.stdout)
    return args.output.exists()


def render_depthflow_inline(args: argparse.Namespace) -> bool:
    try:
        from attrs import define  # type: ignore
        from depthflow.scene import DepthScene  # type: ignore
        from depthflow.state import DepthState  # type: ignore
    except Exception:
        return False

    try:
        @define
        class OrbitScene(DepthScene):  # type: ignore[misc]
            def update(self) -> None:  # noqa: D401 - DepthFlow callback signature
                self.state.height = args.depth_scale
                self.state.steady = 0.30
                self.state.focus = 0.30
                self.state.isometric = 0.60
                self.state.offset = (args.motion_speed * math.sin(self.cycle), 0.0)

        scene = OrbitScene(backend="headless")
        scene.ffmpeg.h264(preset=args.encoder_preset)
        scene.input(image=str(args.input_image.resolve()))
        scene.main(
            output=str(args.output.resolve()),
            time=args.duration,
            width=args.width,
            height=args.height,
            fps=args.fps,
            ssaa=1,
        )
        return args.output.exists()
    except Exception as exc:
        print(f"DepthFlow render failed, using manual fallback: {exc}")
        return False


def fit_cover_image(image: Any, width: int, height: int) -> Any:
    image = image.convert("RGB")
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    from PIL import Image as PILImage  # type: ignore

    lanczos = PILImage.Resampling.LANCZOS if hasattr(PILImage, "Resampling") else PILImage.LANCZOS
    resized = image.resize((math.ceil(src_w * scale), math.ceil(src_h * scale)), resample=lanczos)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def estimate_depth(image_rgb: Any, args: argparse.Namespace, cv2: Any, np: Any, Image: Any) -> tuple[Any, str]:
    depth_height, depth_width = image_rgb.height, image_rgb.width
    if args.depth_method in {"auto", "midas"}:
        try:
            import torch  # type: ignore
            from transformers import DPTForDepthEstimation, DPTImageProcessor  # type: ignore

            processor = DPTImageProcessor.from_pretrained(args.midas_model)
            model = DPTForDepthEstimation.from_pretrained(args.midas_model)
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            model.to(device)
            inputs = processor(images=image_rgb, return_tensors="pt").to(device)
            with torch.no_grad():
                outputs = model(**inputs)
                predicted_depth = outputs.predicted_depth
            prediction = torch.nn.functional.interpolate(
                predicted_depth.unsqueeze(1),
                size=(depth_height, depth_width),
                mode="bicubic",
                align_corners=False,
            ).squeeze()
            depth = prediction.cpu().numpy()
            depth = normalize_depth(depth, np)
            return depth, "midas"
        except Exception as exc:
            if args.depth_method == "midas":
                raise RuntimeError(f"MiDaS depth estimation failed: {exc}") from exc
            print(f"MiDaS unavailable, using image heuristic depth: {exc}")

    image = np.asarray(image_rgb).astype("float32") / 255.0
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    saturation = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)[:, :, 1]
    vertical_near = np.linspace(0.0, 1.0, depth_height, dtype="float32")[:, None]
    center_weight = 1.0 - np.clip(np.abs(np.linspace(-1.0, 1.0, depth_width, dtype="float32"))[None, :], 0, 1)
    detail = cv2.Laplacian(gray, cv2.CV_32F)
    depth = 0.46 * vertical_near + 0.24 * saturation + 0.18 * center_weight + 0.12 * np.abs(detail)
    depth = cv2.GaussianBlur(depth, (0, 0), 9)
    return normalize_depth(depth, np), "heuristic"


def normalize_depth(depth: Any, np: Any) -> Any:
    depth = depth.astype("float32")
    depth -= float(depth.min())
    peak = float(depth.max())
    if peak > 0:
        depth /= peak
    return depth


def layer_masks(depth: Any, cv2: Any, np: Any) -> dict[str, Any]:
    bg_cut, fg_cut = np.quantile(depth, [0.30, 0.70])
    masks = {
        "background": depth <= bg_cut,
        "midground": (depth > bg_cut) & (depth < fg_cut),
        "foreground": depth >= fg_cut,
    }
    refined: dict[str, Any] = {}
    kernel = np.ones((15, 15), np.uint8)
    for name, mask in masks.items():
        alpha = (mask.astype("uint8") * 255)
        alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, kernel)
        alpha = cv2.GaussianBlur(alpha, (0, 0), 9)
        refined[name] = alpha.astype("float32") / 255.0
    return refined


def shift_layer(layer: Any, dx: float, dy: float, cv2: Any, np: Any) -> Any:
    height, width = layer.shape[:2]
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(
        layer,
        matrix,
        (width, height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT,
        borderValue=BACKGROUND_RGB + (0,),
    )


def crop_camera_frame(source: Any, width: int, height: int, pan_x: float, pan_y: float, zoom: float, cv2: Any, np: Any) -> Any:
    buffer_height, buffer_width = source.shape[:2]
    half_crop_w = width / (2.0 * zoom)
    half_crop_h = height / (2.0 * zoom)
    min_center_x = half_crop_w
    max_center_x = buffer_width - half_crop_w
    min_center_y = half_crop_h
    max_center_y = buffer_height - half_crop_h
    center_x = min_center_x + (max_center_x - min_center_x) * max(0.0, min(1.0, pan_x))
    center_y = min_center_y + (max_center_y - min_center_y) * max(0.0, min(1.0, pan_y))
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


def encode_frames(frame_dir: Path, output_path: Path, fps: int, crf: int, encoder_preset: str) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required but was not found on PATH")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_dir / "frame_%05d.png"),
        "-c:v",
        "libx264",
        "-preset",
        encoder_preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]
    subprocess.run(command, check=True)


def render_manual(args: argparse.Namespace) -> str:
    cv2, np, Image = ensure_deps()
    input_image = Image.open(args.input_image)
    buffer_width, buffer_height = buffer_size(args.width, args.height)
    fitted = fit_cover_image(input_image, buffer_width, buffer_height)
    depth, method = estimate_depth(fitted, args, cv2, np, Image)
    masks = layer_masks(depth, cv2, np)
    rgb = np.asarray(fitted).astype("float32")
    layers = {
        name: np.dstack([rgb, masks[name] * 255.0]).astype("float32")
        for name in ("background", "midground", "foreground")
    }
    frame_count = round(args.duration * args.fps)
    motion_curve = precompute_motion_curve(frame_count)
    pan_x_values = precompute_motion_values(args.pan_x_start, args.pan_x_end, motion_curve)
    pan_y_values = precompute_motion_values(args.pan_y_start, args.pan_y_end, motion_curve)
    zoom_values = precompute_motion_values(args.zoom_start, args.zoom_end, motion_curve)
    pan_delta_x_values = precompute_motion_values(0.0, args.parallax_x, motion_curve)
    pan_delta_y_values = precompute_motion_values(0.0, args.parallax_y, motion_curve)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="parallax_frames_") as temp_name:
        frame_dir = Path(temp_name)
        previous_pan_x = pan_x_values[0]
        previous_pan_y = pan_y_values[0]
        for index in range(frame_count):
            canvas = np.full((buffer_height, buffer_width, 3), BACKGROUND_RGB, dtype="float32")
            for name in ("background", "midground", "foreground"):
                multiplier = LAYER_MULTIPLIERS[name]
                shifted = shift_layer(
                    layers[name],
                    pan_delta_x_values[index] * multiplier,
                    pan_delta_y_values[index] * multiplier,
                    cv2,
                    np,
                )
                alpha = shifted[:, :, 3:4] / 255.0
                canvas = shifted[:, :, :3] * alpha + canvas * (1.0 - alpha)
            frame = crop_camera_frame(
                canvas,
                args.width,
                args.height,
                pan_x_values[index],
                pan_y_values[index],
                zoom_values[index],
                cv2,
                np,
            )
            frame = directional_motion_blur(frame, pan_x_values[index] - previous_pan_x, pan_y_values[index] - previous_pan_y, cv2, np)
            previous_pan_x = pan_x_values[index]
            previous_pan_y = pan_y_values[index]
            frame = np.clip(frame, 0, 255).astype("uint8")
            cv2.imwrite(str(frame_dir / f"frame_{index:05d}.png"), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        encode_frames(frame_dir, args.output, args.fps, args.crf, args.encoder_preset)

    sidecar = args.output.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "method": method,
                "depthflow": False,
                "renderer": "opencv_smooth_subpixel_parallax",
                "frames": frame_count,
                "fps": args.fps,
                "size": [args.width, args.height],
                "buffer_size": [buffer_width, buffer_height],
                "easing": "smoothstep",
                "interpolation": "cv2.INTER_LANCZOS4",
                "layer_multipliers": LAYER_MULTIPLIERS,
                "motion_blur_pixels": MOTION_BLUR_PIXELS,
            },
            indent=2,
        )
        + "\n"
    )
    return method


def zoom_frame(frame: Any, zoom: float, y_drift: float, cv2: Any, np: Any) -> Any:
    height, width = frame.shape[:2]
    crop_w = max(2, int(width / zoom))
    crop_h = max(2, int(height / zoom))
    left = (width - crop_w) // 2
    top_center = (height - crop_h) // 2
    top = int(round(top_center + (height - crop_h) * y_drift))
    top = max(0, min(height - crop_h, top))
    crop = frame[top : top + crop_h, left : left + crop_w]
    return cv2.resize(crop, (width, height), interpolation=cv2.INTER_CUBIC)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a 1080x1920 parallax MP4 from one still image.")
    parser.add_argument("input_image", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=DURATION)
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    parser.add_argument("--depth-scale", type=float, default=0.5)
    parser.add_argument("--motion-speed", type=float, default=0.4)
    parser.add_argument("--method", choices=("auto", "depthflow", "manual"), default="auto")
    parser.add_argument("--depth-method", choices=("auto", "midas", "heuristic"), default="auto")
    parser.add_argument("--midas-model", default="Intel/dpt-large")
    parser.add_argument("--pan-x-start", type=float, default=0.5)
    parser.add_argument("--pan-x-end", type=float, default=0.5)
    parser.add_argument("--pan-y-start", type=float, default=0.55)
    parser.add_argument("--pan-y-end", type=float, default=0.45)
    parser.add_argument("--zoom-start", type=float, default=1.0)
    parser.add_argument("--zoom-end", type=float)
    parser.add_argument("--parallax-x", type=float)
    parser.add_argument("--parallax-y", type=float)
    parser.add_argument("--ken-burns-zoom", type=float, default=1.0, help="Deprecated alias for --zoom-end")
    parser.add_argument("--ken-burns-y-drift", type=float, default=0.0)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--encoder-preset", default="medium")
    parser.add_argument("--_depthflow-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.duration <= 0 or args.fps <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("duration, fps, width, and height must be positive")
    if args.zoom_end is None:
        args.zoom_end = args.ken_burns_zoom
    if args.zoom_start < 1.0 or args.zoom_end < 1.0:
        parser.error("zoom values must be >= 1.0")
    if args.parallax_x is None:
        args.parallax_x = -40.0 * args.motion_speed / 0.4
    if args.parallax_y is None:
        args.parallax_y = args.ken_burns_y_drift * -120.0
    args.input_image = args.input_image.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.input_image.exists():
        parser.error(f"input image not found: {args.input_image}")
    return args


def main() -> None:
    args = parse_args()
    if args._depthflow_child:
        if not render_depthflow_inline(args):
            raise RuntimeError("DepthFlow child render failed")
        return

    used_depthflow = False
    if args.method in {"auto", "depthflow"}:
        used_depthflow = run_depthflow(args)
        if args.method == "depthflow" and not used_depthflow:
            raise RuntimeError("DepthFlow requested but failed or is not installed")
    if used_depthflow:
        args.output.with_suffix(".json").write_text(json.dumps({"method": "depthflow", "depthflow": True}, indent=2) + "\n")
        print(f"{args.output} (depthflow)")
    else:
        method = render_manual(args)
        print(f"{args.output} ({method})")


if __name__ == "__main__":
    main()
