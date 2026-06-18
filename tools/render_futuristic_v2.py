#!/usr/bin/env python3
"""Render DailyZ futuristic v2 seamless background loops.

The renderer follows the R2 creative spec at:
3 - Production/creative_direction/futuristic_v2_specs.md

It renders deterministic 8s, 30fps, 1080x1920 H.264 MP4 loops. Frames are
drawn procedurally at 540x960, then upscaled with FFmpeg Lanczos filtering.

R2 upload is optional and uses S3-compatible environment variables:
R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image  # Imported intentionally: PIL is part of the approved stack.

try:
    import boto3
    from botocore.config import Config
except ImportError:  # pragma: no cover - upload mode reports this clearly.
    boto3 = None
    Config = None

try:
    import moviepy  # noqa: F401 - keeps the script compatible with the requested stack.
except ImportError:  # pragma: no cover - render path uses OpenCV/FFmpeg directly.
    moviepy = None


WIDTH, HEIGHT = 1080, 1920
DRAW_W, DRAW_H = 540, 960
SCALE = DRAW_W / WIDTH
DURATION = 8.0
FPS = 30
FRAME_COUNT = int(DURATION * FPS)

BUCKET = "cigaler-assets"
R2_PREFIX = "2 - Library/loops/futuristic"

SEEDS = {
    "futuristic_001_orbital_relay": 4201,
    "futuristic_002_signal_aurora": 4202,
    "futuristic_003_quantum_rain": 4203,
    "futuristic_004_topology_drift": 4204,
    "futuristic_005_particle_bloom": 4205,
}

PALETTE = {
    "base_black": "#0A0A0F",
    "lifted_black": "#101018",
    "deep_indigo": "#151A2D",
    "graphite_blue": "#242A3D",
    "signal_blue": "#2F6BFF",
    "premium_violet": "#7C5CFF",
    "soft_mint": "#5EF5D4",
    "caption_cyan": "#00E5FF",
    "soft_white": "#EAFBFF",
}


@dataclass(frozen=True)
class Particle:
    mode: str
    cx: float
    cy: float
    rx: float
    ry: float
    speed: float
    angle0: float
    z: float
    radius: float
    alpha: float
    color: tuple[int, int, int]
    twinkle: float
    direction: float = 1.0
    cluster: int = -1


@dataclass(frozen=True)
class Ribbon:
    p0: tuple[float, float]
    p1: tuple[float, float]
    p2: tuple[float, float]
    p3: tuple[float, float]
    color: tuple[int, int, int]
    alpha: float
    width: float
    amp: float
    phase: float
    style: str


@dataclass(frozen=True)
class Bead:
    ribbon: int
    u0: float
    speed: float
    phase: float
    color: tuple[int, int, int]
    core_radius: float
    glow_radius: float
    alpha: float
    tail_points: int
    direction: float = 1.0


@dataclass(frozen=True)
class Wash:
    x: float
    y: float
    radius: float
    color: tuple[int, int, int]
    alpha: float
    phase: float


@dataclass(frozen=True)
class Variation:
    slug: str
    output_name: str
    far: list[Particle]
    mid: list[Particle]
    ribbons: list[Ribbon]
    beads: list[Bead]
    washes: list[Wash]
    cluster_centers: list[tuple[float, float]]


def rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


COLORS = {name: rgb(value) for name, value in PALETTE.items()}


def caption_safety_multiplier(x: float, y: float) -> float:
    in_x = 80 <= x <= 1000
    in_y = 1120 <= y <= 1700
    if in_x and in_y:
        return 0.45
    if 980 <= y <= 1760:
        return 0.70
    return 1.0


def screen_blend(base: np.ndarray, glow: np.ndarray) -> np.ndarray:
    base_f = base.astype(np.float32) / 255.0
    glow_f = np.clip(glow, 0, 255).astype(np.float32) / 255.0
    out = 1.0 - (1.0 - base_f) * (1.0 - glow_f)
    return np.clip(out * 255, 0, 255).astype(np.uint8)


def blend_emission(frame: np.ndarray, emission: np.ndarray) -> np.ndarray:
    return np.clip(frame.astype(np.float32) + emission, 0, 255).astype(np.uint8)


def blur_layer(layer: np.ndarray, sigma_px: float) -> np.ndarray:
    sigma = max(0.1, sigma_px * SCALE)
    ksize = max(3, int(math.ceil(sigma * 6)) | 1)
    return cv2.GaussianBlur(layer, (ksize, ksize), sigmaX=sigma, sigmaY=sigma)


def make_base(seed: int) -> np.ndarray:
    base = np.zeros((DRAW_H, DRAW_W, 3), dtype=np.float32)
    base[:, :] = COLORS["base_black"]

    yy, xx = np.mgrid[0:DRAW_H, 0:DRAW_W].astype(np.float32)
    cx, cy = DRAW_W * 0.48, DRAW_H * 0.42
    nx = (xx - cx) / (DRAW_W * 0.72)
    ny = (yy - cy) / (DRAW_H * 0.68)
    dist = np.sqrt(nx * nx + ny * ny)
    lift = np.interp(dist, [0.0, 0.72, 1.35], [8.0, 0.0, -10.0])
    base += lift[..., None]

    rng = np.random.default_rng(seed + 9000)
    noise = rng.normal(0.0, 1.0, (DRAW_H, DRAW_W, 1)).astype(np.float32)
    base += noise * (255.0 * 0.018)
    return np.clip(base, 0, 255).astype(np.uint8)


def sxy(point: tuple[float, float]) -> tuple[int, int]:
    return int(round(point[0] * SCALE)), int(round(point[1] * SCALE))


def add_circle(
    layer: np.ndarray,
    x: float,
    y: float,
    radius: float,
    color: tuple[int, int, int],
    alpha: float,
) -> None:
    if radius <= 0 or alpha <= 0:
        return
    px, py = sxy((x, y))
    pr = max(1, int(round(radius * SCALE)))
    if px < -pr or px >= DRAW_W + pr or py < -pr or py >= DRAW_H + pr:
        return
    draw_color = tuple(float(channel) * alpha for channel in color)
    cv2.circle(layer, (px, py), pr, draw_color, -1, lineType=cv2.LINE_AA)


def add_polyline(
    layer: np.ndarray,
    points: np.ndarray,
    color: tuple[int, int, int],
    alpha: float,
    width: float,
) -> None:
    if len(points) < 2 or alpha <= 0:
        return
    thickness = max(1, int(round(width * SCALE)))
    for start, end in zip(points[:-1], points[1:]):
        mx = float((start[0] + end[0]) * 0.5)
        my = float((start[1] + end[1]) * 0.5)
        safe_alpha = alpha * caption_safety_multiplier(mx, my)
        if safe_alpha <= 0:
            continue
        cv2.line(
            layer,
            sxy((float(start[0]), float(start[1]))),
            sxy((float(end[0]), float(end[1]))),
            tuple(float(channel) * safe_alpha for channel in color),
            thickness,
            lineType=cv2.LINE_AA,
        )


def cubic_bezier(ribbon: Ribbon, phase: float, samples: int = 120) -> np.ndarray:
    u = np.linspace(0.0, 1.0, samples, dtype=np.float32)
    p0 = np.array(ribbon.p0, dtype=np.float32)
    p1 = np.array(ribbon.p1, dtype=np.float32)
    p2 = np.array(ribbon.p2, dtype=np.float32)
    p3 = np.array(ribbon.p3, dtype=np.float32)
    omt = 1.0 - u
    base = (
        (omt**3)[:, None] * p0
        + (3 * omt * omt * u)[:, None] * p1
        + (3 * omt * u * u)[:, None] * p2
        + (u**3)[:, None] * p3
    )
    wobble = ribbon.amp * np.sin(2 * np.pi * u + phase + ribbon.phase)
    secondary = 0.35 * ribbon.amp * np.sin(4 * np.pi * u - phase + ribbon.phase)
    if ribbon.style == "aurora":
        base[:, 0] += wobble + 0.45 * secondary
        base[:, 1] += 0.20 * secondary
    elif ribbon.style == "rain":
        base[:, 0] += 0.55 * wobble
        base[:, 1] += 0.30 * secondary
    elif ribbon.style == "topology":
        base[:, 0] += 0.30 * wobble
        base[:, 1] += wobble + secondary
    else:
        base[:, 0] += 0.25 * wobble
        base[:, 1] += wobble + secondary
    return base


def point_on_path(points: np.ndarray, u: float) -> tuple[float, float]:
    wrapped = u % 1.0
    position = wrapped * (len(points) - 1)
    idx = int(position)
    frac = position - idx
    nxt = min(idx + 1, len(points) - 1)
    point = points[idx] * (1.0 - frac) + points[nxt] * frac
    return float(point[0]), float(point[1])


def particle_position(particle: Particle, phase: float, progress: float, centers: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    if particle.mode == "rain":
        angle = particle.angle0 + particle.speed * phase
        x = particle.cx + particle.rx * np.sin(angle) + 18 * particle.z * np.sin(2 * phase + particle.twinkle)
        y = particle.cy + particle.ry * np.sin(angle - 0.85) + 34 * particle.direction * progress
    elif particle.mode == "cluster":
        center = centers[particle.cluster]
        local_phase = phase + particle.twinkle
        breath = 0.5 - 0.5 * np.cos(local_phase)
        radius = 42.0 + (110.0 - 42.0) * breath
        angle = particle.angle0 + particle.speed * phase
        x = center[0] + radius * particle.rx * np.cos(angle)
        y = center[1] + radius * particle.ry * np.sin(angle)
    else:
        angle = particle.angle0 + particle.speed * phase
        x = particle.cx + particle.rx * np.cos(angle) + 10 * particle.z * np.sin(phase + particle.angle0)
        y = particle.cy + particle.ry * np.sin(angle) + 16 * particle.z * np.cos(phase + particle.angle0)
    alpha = particle.alpha * (0.72 + 0.28 * np.sin(phase + particle.twinkle))
    radius = particle.radius * (0.85 + 0.25 * particle.z + 0.10 * np.sin(phase + particle.angle0))
    return float(x), float(y), float(max(0.0, alpha)), float(max(0.5, radius))


def build_particles(
    rng: np.random.Generator,
    count: int,
    mode: str,
    palette: list[tuple[int, int, int]],
    far: bool,
    centers: list[tuple[float, float]] | None = None,
) -> list[Particle]:
    particles: list[Particle] = []
    for index in range(count):
        z = float(rng.uniform(0.0, 0.45) if far else rng.uniform(0.45, 1.0))
        if mode == "orbital_relay":
            attractor = [(-180.0, 560.0), (1260.0, 1260.0)][index % 2]
            cx = attractor[0] + rng.uniform(-70, 70)
            cy = attractor[1] + rng.uniform(-100, 100)
            rx = rng.uniform(240, 780) if far else rng.uniform(360, 900)
            ry = rng.uniform(140, 620) if far else rng.uniform(220, 720)
        elif mode == "aurora":
            cx = rng.uniform(420, 860)
            cy = rng.uniform(150, 1780)
            rx = rng.uniform(18, 90) if far else rng.uniform(45, 180)
            ry = rng.uniform(16, 80) if far else rng.uniform(35, 150)
        elif mode == "rain":
            cx = rng.uniform(70, 1010)
            cy = rng.uniform(80, 1840)
            rx = rng.uniform(12, 50) if far else rng.uniform(24, 80)
            ry = rng.uniform(28, 74) if far else rng.uniform(42, 118)
        elif mode == "topology":
            cx = rng.choice([rng.uniform(40, 270), rng.uniform(760, 1040), rng.uniform(100, 980)])
            cy = rng.choice([rng.uniform(80, 700), rng.uniform(760, 1040), rng.uniform(1520, 1840)])
            rx = rng.uniform(12, 70) if far else rng.uniform(30, 120)
            ry = rng.uniform(8, 45) if far else rng.uniform(18, 86)
        elif mode == "cluster":
            cx = 0.0
            cy = 0.0
            rx = rng.uniform(0.35, 1.0)
            ry = rng.uniform(0.35, 0.92)
        else:
            cx = rng.uniform(0, WIDTH)
            cy = rng.uniform(0, HEIGHT)
            rx = rng.uniform(20, 220)
            ry = rng.uniform(10, 120)
        speed_choices = [-1.0, 1.0] if far else [-2.0, -1.0, 1.0, 2.0]
        speed = float(rng.choice(speed_choices))
        if mode == "cluster":
            speed *= rng.uniform(0.35, 0.75)
        alpha = float(rng.uniform(0.035, 0.11) if far else rng.uniform(0.08, 0.22))
        radius = float(rng.uniform(0.8, 1.6) if far else rng.uniform(1.5, 3.2))
        cluster = -1 if not centers else int(index % len(centers))
        particles.append(
            Particle(
                mode="cluster" if mode == "cluster" else ("rain" if mode == "rain" else "orbit"),
                cx=float(cx),
                cy=float(cy),
                rx=float(rx),
                ry=float(ry),
                speed=speed,
                angle0=float(rng.uniform(0, 2 * np.pi)),
                z=z,
                radius=radius,
                alpha=alpha,
                color=palette[int(rng.integers(0, len(palette)))],
                twinkle=float(rng.uniform(0, 2 * np.pi)),
                direction=float(rng.choice([-1.0, 1.0])) if mode == "rain" else 1.0,
                cluster=cluster,
            )
        )
    return particles


def make_diagonal_ribbons(rng: np.random.Generator, count: int, palette: list[tuple[int, int, int]]) -> list[Ribbon]:
    ribbons = []
    for i in range(count):
        y0 = rng.uniform(1230, 1920) if i % 2 == 0 else rng.uniform(-120, 460)
        y3 = rng.uniform(-140, 620) if i % 2 == 0 else rng.uniform(1220, 2040)
        p0 = (-140.0, float(y0)) if i % 2 == 0 else (1220.0, float(y0))
        p3 = (1220.0, float(y3)) if i % 2 == 0 else (-140.0, float(y3))
        ribbons.append(
            Ribbon(
                p0=p0,
                p1=(float(rng.uniform(150, 420)), float(rng.uniform(820, 1480))),
                p2=(float(rng.uniform(650, 970)), float(rng.uniform(420, 1100))),
                p3=p3,
                color=palette[i % len(palette)],
                alpha=float(rng.uniform(0.065, 0.13)),
                width=float(rng.uniform(1.5, 3.2)),
                amp=float(rng.uniform(8, 22)),
                phase=float(rng.uniform(0, 2 * np.pi)),
                style="diagonal",
            )
        )
    return ribbons


def make_aurora_ribbons(rng: np.random.Generator, count: int, palette: list[tuple[int, int, int]]) -> list[Ribbon]:
    ribbons = []
    for i in range(count):
        side_left = i % 2 == 0
        x = float(rng.uniform(80, 210) if side_left else rng.uniform(875, 1010))
        p0 = (x, -100.0)
        p3 = (x + rng.uniform(-35, 35), 2020.0)
        ribbons.append(
            Ribbon(
                p0=p0,
                p1=(x + rng.uniform(-100, 110), float(rng.uniform(380, 620))),
                p2=(x + rng.uniform(-120, 120), float(rng.uniform(1250, 1580))),
                p3=p3,
                color=palette[i % len(palette)],
                alpha=float(rng.uniform(0.055, 0.12)),
                width=float(rng.uniform(1.3, 2.7)),
                amp=float(rng.uniform(12, 26)),
                phase=float(rng.uniform(0, 2 * np.pi)),
                style="aurora",
            )
        )
    return ribbons


def make_rain_ribbons(rng: np.random.Generator, count: int, palette: list[tuple[int, int, int]]) -> list[Ribbon]:
    ribbons = []
    for i in range(count):
        x = float(rng.uniform(170, 930))
        ribbons.append(
            Ribbon(
                p0=(x, -120.0),
                p1=(x + rng.uniform(-130, 90), 520.0),
                p2=(x + rng.uniform(-90, 130), 1320.0),
                p3=(x + rng.uniform(-80, 80), 2040.0),
                color=palette[i % len(palette)],
                alpha=float(rng.uniform(0.045, 0.075)),
                width=float(rng.uniform(1.0, 1.8)),
                amp=float(rng.uniform(8, 18)),
                phase=float(rng.uniform(0, 2 * np.pi)),
                style="rain",
            )
        )
    return ribbons


def make_topology_ribbons(rng: np.random.Generator, count: int, palette: list[tuple[int, int, int]]) -> list[Ribbon]:
    ribbons = []
    bands = [rng.uniform(140, 620), rng.uniform(180, 700), rng.uniform(220, 760), rng.uniform(250, 720)]
    for i in range(count):
        cluster_y = float(bands[i % len(bands)] if i < 6 else rng.choice([rng.uniform(900, 1120), rng.uniform(70, 520)]))
        left = i % 3 == 0
        start_x = float(rng.uniform(-130, 160) if left else rng.uniform(760, 1040))
        end_x = float(rng.uniform(220, 760) if left else rng.uniform(980, 1210))
        ribbons.append(
            Ribbon(
                p0=(start_x, cluster_y + rng.uniform(-60, 60)),
                p1=(float(rng.uniform(170, 420)), cluster_y + rng.uniform(-110, 80)),
                p2=(float(rng.uniform(600, 910)), cluster_y + rng.uniform(-90, 110)),
                p3=(end_x, cluster_y + rng.uniform(-60, 60)),
                color=palette[i % len(palette)],
                alpha=float(rng.uniform(0.04, 0.095)),
                width=float([1.0, 1.5, 2.0][i % 3]),
                amp=float(rng.uniform(7, 20)),
                phase=float(rng.uniform(0, 2 * np.pi)),
                style="topology",
            )
        )
    return ribbons


def make_bloom_ribbons(rng: np.random.Generator, palette: list[tuple[int, int, int]]) -> list[Ribbon]:
    return [
        Ribbon((-120, 360), (260, 650), (690, 760), (1220, 980), palette[0], 0.045, 1.4, 10, rng.uniform(0, 2 * np.pi), "diagonal"),
        Ribbon((1180, 1640), (760, 1430), (420, 1220), (-140, 1180), palette[1], 0.038, 1.2, 12, rng.uniform(0, 2 * np.pi), "diagonal"),
    ]


def make_beads(rng: np.random.Generator, count: int, ribbon_count: int, palette: list[tuple[int, int, int]], alpha_cap: float, quantum: bool = False) -> list[Bead]:
    beads = []
    for i in range(count):
        direction = -1.0 if quantum and i < max(1, count // 5) else 1.0
        beads.append(
            Bead(
                ribbon=i % ribbon_count,
                u0=float(rng.uniform(0, 1)),
                speed=float(rng.choice([1.0, 2.0])) * direction,
                phase=float(rng.uniform(0, 2 * np.pi)),
                color=palette[i % len(palette)],
                core_radius=float(rng.uniform(2.0, 4.0)),
                glow_radius=float(rng.uniform(18.0, 42.0)),
                alpha=float(rng.uniform(0.16, alpha_cap)),
                tail_points=int(rng.integers(8, 15) if quantum else rng.integers(4, 8)),
                direction=direction,
            )
        )
    return beads


def build_variation(slug: str) -> Variation:
    rng = np.random.default_rng(SEEDS[slug])
    blue = COLORS["signal_blue"]
    violet = COLORS["premium_violet"]
    mint = COLORS["soft_mint"]
    graphite = COLORS["graphite_blue"]
    white = COLORS["soft_white"]

    if slug == "futuristic_001_orbital_relay":
        ribbons = make_diagonal_ribbons(rng, 4, [blue, violet, blue, violet])
        return Variation(
            slug,
            "futuristic_v2_001.mp4",
            build_particles(rng, 100, "orbital_relay", [graphite, graphite, blue], True),
            build_particles(rng, 48, "orbital_relay", [blue, violet], False),
            ribbons,
            make_beads(rng, 6, len(ribbons), [blue, violet], 0.22),
            [Wash(-160, 480, 360, blue, 0.035, rng.uniform(0, 2 * np.pi)), Wash(1240, 1300, 430, violet, 0.032, rng.uniform(0, 2 * np.pi))],
            [],
        )
    if slug == "futuristic_002_signal_aurora":
        ribbons = make_aurora_ribbons(rng, 7, [mint, blue, mint, violet, blue, mint, blue])
        return Variation(
            slug,
            "futuristic_v2_002.mp4",
            build_particles(rng, 70, "aurora", [graphite, graphite, blue], True),
            build_particles(rng, 34, "aurora", [mint, blue, violet], False),
            ribbons,
            make_beads(rng, 8, len(ribbons), [mint, blue, violet], 0.18),
            [Wash(-120, 520, 470, mint, 0.028, rng.uniform(0, 2 * np.pi)), Wash(1210, 1120, 510, blue, 0.026, rng.uniform(0, 2 * np.pi))],
            [],
        )
    if slug == "futuristic_003_quantum_rain":
        ribbons = make_rain_ribbons(rng, 3, [blue, mint, blue])
        return Variation(
            slug,
            "futuristic_v2_003.mp4",
            build_particles(rng, 130, "rain", [graphite, graphite, blue, mint], True),
            build_particles(rng, 32, "rain", [blue, mint], False),
            ribbons,
            make_beads(rng, 10, len(ribbons), [blue, mint], 0.22, quantum=True),
            [Wash(1150, 360, 360, blue, 0.022, rng.uniform(0, 2 * np.pi))],
            [],
        )
    if slug == "futuristic_004_topology_drift":
        ribbons = make_topology_ribbons(rng, 9, [violet, graphite, blue, violet, graphite, violet, blue, graphite, violet])
        return Variation(
            slug,
            "futuristic_v2_004.mp4",
            build_particles(rng, 55, "topology", [graphite, graphite, violet], True),
            build_particles(rng, 26, "topology", [violet, blue, graphite], False),
            ribbons,
            make_beads(rng, 5, len(ribbons), [violet, blue], 0.18),
            [Wash(980, 260, 320, violet, 0.024, rng.uniform(0, 2 * np.pi))],
            [],
        )
    if slug == "futuristic_005_particle_bloom":
        centers = [(180, 360), (850, 420), (960, 920), (210, 1320), (760, 1780)]
        ribbons = make_bloom_ribbons(rng, [blue, violet])
        return Variation(
            slug,
            "futuristic_v2_005.mp4",
            build_particles(rng, 80, "default", [graphite, graphite, blue], True),
            build_particles(rng, 65, "cluster", [white, violet, blue], False, centers),
            ribbons,
            make_beads(rng, 4, len(ribbons), [white, violet, blue], 0.20),
            [Wash(180, 360, 310, violet, 0.034, rng.uniform(0, 2 * np.pi)), Wash(850, 420, 340, blue, 0.029, rng.uniform(0, 2 * np.pi))],
            centers,
        )
    raise ValueError(f"Unknown variation: {slug}")


def render_frame(variation: Variation, base: np.ndarray, frame_index: int) -> np.ndarray:
    t = frame_index / FPS
    progress = t / DURATION
    phase = 2 * np.pi * progress
    frame = base.copy()
    sharp = np.zeros((DRAW_H, DRAW_W, 3), dtype=np.float32)
    glow_small = np.zeros_like(sharp)
    glow_medium = np.zeros_like(sharp)
    glow_large = np.zeros_like(sharp)

    for wash in variation.washes:
        offset = 20 * math.sin(phase + wash.phase)
        add_circle(glow_large, wash.x + offset, wash.y - 0.6 * offset, wash.radius, wash.color, wash.alpha)

    for particle in variation.far:
        x, y, alpha, radius = particle_position(particle, phase, progress, variation.cluster_centers)
        safe_alpha = alpha * caption_safety_multiplier(x, y)
        add_circle(sharp, x, y, radius, particle.color, safe_alpha)

    for particle in variation.mid:
        x, y, alpha, radius = particle_position(particle, phase, progress, variation.cluster_centers)
        safe_alpha = alpha * caption_safety_multiplier(x, y)
        add_circle(sharp, x, y, radius, particle.color, safe_alpha)
        add_circle(glow_small, x, y, radius * 3.8, particle.color, safe_alpha * 0.11)
        if variation.slug == "futuristic_005_particle_bloom":
            add_circle(glow_medium, x, y, radius * 8.0, particle.color, safe_alpha * 0.055)

    paths = [cubic_bezier(ribbon, phase) for ribbon in variation.ribbons]
    for ribbon, points in zip(variation.ribbons, paths):
        add_polyline(sharp, points, ribbon.color, ribbon.alpha, ribbon.width)
        add_polyline(glow_small, points, ribbon.color, ribbon.alpha * 0.38, ribbon.width + 2.0)
        if variation.slug != "futuristic_004_topology_drift":
            add_polyline(glow_medium, points, ribbon.color, ribbon.alpha * 0.16, ribbon.width + 5.0)
        if variation.slug == "futuristic_002_signal_aurora":
            add_polyline(glow_large, points, ribbon.color, ribbon.alpha * 0.055, ribbon.width + 9.0)

    for bead in variation.beads:
        path = paths[bead.ribbon]
        u = (bead.u0 + bead.speed * progress) % 1.0
        head_alpha = bead.alpha * (0.65 + 0.35 * math.sin(phase + bead.phase))
        for tail_index in range(bead.tail_points):
            tail_u = (u - bead.speed * 0.010 * tail_index) % 1.0
            x, y = point_on_path(path, tail_u)
            fade = (1.0 - tail_index / max(1, bead.tail_points)) ** 1.45
            safe_alpha = head_alpha * fade * caption_safety_multiplier(x, y)
            add_circle(sharp, x, y, bead.core_radius * (1.0 - 0.035 * tail_index), bead.color, safe_alpha * (0.34 if tail_index else 1.0))
            add_circle(glow_small, x, y, bead.glow_radius * (0.32 + 0.035 * tail_index), bead.color, safe_alpha * 0.13)
        hx, hy = point_on_path(path, u)
        safe = caption_safety_multiplier(hx, hy)
        add_circle(glow_medium, hx, hy, bead.glow_radius, bead.color, min(0.06, head_alpha * 0.22) * safe)
        add_circle(glow_large, hx, hy, bead.glow_radius * 1.55, bead.color, min(0.032, head_alpha * 0.10) * safe)

    frame = blend_emission(frame, sharp)
    frame = screen_blend(frame, blur_layer(glow_small, 4.0) * 0.45)
    frame = screen_blend(frame, blur_layer(glow_medium, 14.0) * 0.22)
    frame = screen_blend(frame, blur_layer(glow_large, 38.0) * 0.08)

    luma = frame[..., 0] * 0.2126 + frame[..., 1] * 0.7152 + frame[..., 2] * 0.0722
    too_hot = luma > 96
    if np.any(too_hot):
        factor = np.ones_like(luma, dtype=np.float32)
        factor[too_hot] = 96.0 / luma[too_hot]
        frame = np.clip(frame.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)
    return frame


def render_variation(variation: Variation, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / variation.output_name
    base = make_base(SEEDS[variation.slug])
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{DRAW_W}x{DRAW_H}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-vf",
        f"scale={WIDTH}:{HEIGHT}:flags=lanczos",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    for frame_index in range(FRAME_COUNT):
        frame = render_frame(variation, base, frame_index)
        process.stdin.write(frame.tobytes())
    process.stdin.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"ffmpeg failed for {variation.output_name}:\n{stderr}")
    Image.open(output_path) if False else None
    return output_path


def r2_client():
    if boto3 is None or Config is None:
        raise RuntimeError("boto3/botocore are required for --upload")
    endpoint = os.environ.get("R2_ENDPOINT")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not endpoint or not access_key or not secret_key:
        raise RuntimeError("Set R2_ENDPOINT, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY for --upload")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload_file(client, local_path: Path) -> str:
    key = f"{R2_PREFIX}/{local_path.name}"
    client.upload_file(str(local_path), BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    client.head_object(Bucket=BUCKET, Key=key)
    return key


def selected_slugs(selection: Iterable[str]) -> list[str]:
    requested = list(selection)
    if not requested:
        return list(SEEDS)
    aliases = {"001": "futuristic_001_orbital_relay", "002": "futuristic_002_signal_aurora", "003": "futuristic_003_quantum_rain", "004": "futuristic_004_topology_drift", "005": "futuristic_005_particle_bloom"}
    slugs = []
    for item in requested:
        slug = aliases.get(item, item)
        if slug not in SEEDS:
            raise ValueError(f"Unknown variation selection: {item}")
        slugs.append(slug)
    return slugs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render futuristic v2 loop variations.")
    parser.add_argument("--output-dir", type=Path, default=Path("output/futuristic_v2"))
    parser.add_argument("--only", nargs="*", default=[], help="Render selected variations: 001..005 or full slugs")
    parser.add_argument("--skip-render", action="store_true", help="Upload existing files from --output-dir")
    parser.add_argument("--upload", action="store_true", help="Upload rendered files to R2 and confirm with head_object")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    slugs = selected_slugs(args.only)
    variations = [build_variation(slug) for slug in slugs]

    rendered: list[Path] = []
    if args.skip_render:
        rendered = [args.output_dir / variation.output_name for variation in variations]
        missing = [path for path in rendered if not path.exists()]
        if missing:
            raise FileNotFoundError(f"Missing files for --skip-render: {missing}")
    else:
        for variation in variations:
            path = render_variation(variation, args.output_dir)
            print(f"rendered {variation.slug}: {path}", flush=True)
            rendered.append(path)

    if args.upload:
        client = r2_client()
        for path in rendered:
            key = upload_file(client, path)
            print(f"uploaded {path.name}: s3://{BUCKET}/{key}", flush=True)


if __name__ == "__main__":
    main()
