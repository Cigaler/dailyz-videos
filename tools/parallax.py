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


def ensure_deps() -> tuple[Any, Any, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - runtime environment check
        raise RuntimeError("manual parallax fallback requires pillow, numpy, and opencv-python-headless") from exc
    return cv2, np, Image


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
    resized = image.resize((math.ceil(src_w * scale), math.ceil(src_h * scale)))
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def estimate_depth(image_rgb: Any, args: argparse.Namespace, cv2: Any, np: Any, Image: Any) -> tuple[Any, str]:
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
                size=(args.height, args.width),
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
    vertical_near = np.linspace(0.0, 1.0, args.height, dtype="float32")[:, None]
    center_weight = 1.0 - np.clip(np.abs(np.linspace(-1.0, 1.0, args.width, dtype="float32"))[None, :], 0, 1)
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


def shift_layer(layer: Any, dx: int, dy: int, cv2: Any, np: Any) -> Any:
    height, width = layer.shape[:2]
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(layer, matrix, (width, height), borderMode=cv2.BORDER_REFLECT)


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
    fitted = fit_cover_image(input_image, args.width, args.height)
    depth, method = estimate_depth(fitted, args, cv2, np, Image)
    masks = layer_masks(depth, cv2, np)
    rgb = np.asarray(fitted).astype("float32")
    layers = {
        name: np.dstack([rgb, masks[name] * 255.0]).astype("float32")
        for name in ("background", "midground", "foreground")
    }
    frame_count = round(args.duration * args.fps)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="parallax_frames_") as temp_name:
        frame_dir = Path(temp_name)
        for index in range(frame_count):
            progress = index / max(frame_count - 1, 1)
            shifts = {
                "background": int(round(-20 * progress * args.motion_speed / 0.4)),
                "midground": int(round(-40 * progress * args.motion_speed / 0.4)),
                "foreground": int(round(-70 * progress * args.motion_speed / 0.4)),
            }
            canvas = np.full((args.height, args.width, 3), BACKGROUND_RGB, dtype="float32")
            for name in ("background", "midground", "foreground"):
                shifted = shift_layer(layers[name], shifts[name], 0, cv2, np)
                alpha = shifted[:, :, 3:4] / 255.0
                canvas = shifted[:, :, :3] * alpha + canvas * (1.0 - alpha)
            if args.ken_burns_zoom != 1.0:
                zoom = 1.0 + (args.ken_burns_zoom - 1.0) * progress
                canvas = zoom_frame(canvas, zoom, args.ken_burns_y_drift * progress, cv2, np)
            frame = np.clip(canvas, 0, 255).astype("uint8")
            cv2.imwrite(str(frame_dir / f"frame_{index:05d}.png"), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        encode_frames(frame_dir, args.output, args.fps, args.crf, args.encoder_preset)

    sidecar = args.output.with_suffix(".json")
    sidecar.write_text(json.dumps({"method": method, "depthflow": False, "frames": frame_count}, indent=2) + "\n")
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
    parser.add_argument("--ken-burns-zoom", type=float, default=1.0)
    parser.add_argument("--ken-burns-y-drift", type=float, default=0.0)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--encoder-preset", default="medium")
    parser.add_argument("--_depthflow-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.duration <= 0 or args.fps <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("duration, fps, width, and height must be positive")
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
