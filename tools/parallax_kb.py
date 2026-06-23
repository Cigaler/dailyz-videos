#!/usr/bin/env python3
"""Render a hybrid parallax + Ken Burns slow push animation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render parallax depth motion with a Ken Burns push layered on top.")
    parser.add_argument("input_image", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--motion-speed", type=float, default=0.4)
    parser.add_argument("--zoom-end", type=float, default=1.15)
    parser.add_argument("--y-drift", type=float, default=0.35)
    parser.add_argument("--method", choices=("auto", "depthflow", "manual"), default="manual")
    parser.add_argument("--depth-method", choices=("auto", "midas", "heuristic"), default="auto")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--encoder-preset", default="medium")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    script = Path(__file__).with_name("parallax.py")
    command = [
        sys.executable,
        str(script),
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
        "--motion-speed",
        str(args.motion_speed),
        "--ken-burns-zoom",
        str(args.zoom_end),
        "--ken-burns-y-drift",
        str(args.y_drift),
        "--method",
        args.method,
        "--depth-method",
        args.depth_method,
        "--crf",
        str(args.crf),
        "--encoder-preset",
        args.encoder_preset,
    ]
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
