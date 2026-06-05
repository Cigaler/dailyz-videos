#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

from PIL import Image, ImageDraw, ImageFont


CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
FPS = 30
BACKGROUND = (10, 10, 15, 255)
WHITE = (255, 255, 255, 255)
CYAN = (0, 229, 255, 255)
MUTED = (173, 182, 191, 255)
CARD = (13, 18, 28, 228)
SAFE_LEFT = 80
SAFE_RIGHT = CANVAS_WIDTH - 80
SAFE_TOP = 200
SAFE_BOTTOM = CANVAS_HEIGHT - 300
SAFE_WIDTH = SAFE_RIGHT - SAFE_LEFT
CAPTION_START_Y = 1200
CAPTION_LINE_GAP = 24
BODY_FONT_MAX = 72
BODY_FONT_MIN = 40
HOOK_FONT_MAX = 96
HOOK_FONT_MIN = 58
CTA_FONT_MAX = 96
CTA_FONT_MIN = 48
SILENCE_GAP_SECONDS = 0.4
HOOK_MIN_SECONDS = 3.0

THUMB_WIDTH = 1024
THUMB_HEIGHT = 1792
THUMB_SAFE_LEFT = 72
THUMB_SAFE_RIGHT = THUMB_WIDTH - 72
THUMB_SAFE_WIDTH = THUMB_SAFE_RIGHT - THUMB_SAFE_LEFT
THUMB_TEXT_TOP = 120
THUMB_TEXT_LINE_GAP = 22
THUMB_FONT_MAX = 84
THUMB_FONT_MIN = 42


class CaptionLayout(NamedTuple):
    font: ImageFont.FreeTypeFont
    font_size: int
    lines: list[list[str]]
    line_widths: list[float]
    line_heights: list[float]
    total_height: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render DailyZ Batch 06 videos and thumbnails.")
    parser.add_argument(
        "--scripts",
        default="scripts/batch_06_scripts.json",
        help="Path to the batch scripts JSON file.",
    )
    parser.add_argument(
        "--output-root",
        default="output/generated/batch_06",
        help="Directory for intermediate render assets and reports.",
    )
    parser.add_argument(
        "--publish-root",
        default="0-to_publish",
        help="Directory for final published MP4/JPG outputs.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        help="Optional list of video IDs to render.",
    )
    return parser.parse_args()


def load_scripts(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())


def select_font_path() -> Path:
    candidates = [
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No supported bold system font found.")


def run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(command)
            + "\nstdout:\n"
            + completed.stdout
            + "\nstderr:\n"
            + completed.stderr
        )


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def api_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    data = None
    request_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url=url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, body


def eleven_request(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    return api_request(method=method, url=url, headers={"xi-api-key": api_key}, payload=payload)


def openai_request(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    return api_request(
        method=method,
        url=url,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=payload,
    )


def resolve_voice(api_key: str) -> tuple[str, str, str]:
    preferred_id = "iP95p4xoKVk53GoZ742B"
    preferred_name = "Chris - Charming, Down-to-Earth"
    status, body = eleven_request("GET", f"https://api.elevenlabs.io/v1/voices/{preferred_id}", api_key)
    if status == 200:
        payload = json.loads(body)
        return payload["voice_id"], payload["name"], "preferred voice id accepted"

    status, body = eleven_request("GET", "https://api.elevenlabs.io/v1/voices", api_key)
    if status != 200:
        raise RuntimeError(
            "Could not list ElevenLabs voices after preferred voice lookup failed: "
            f"HTTP {status} {body.decode('utf-8', errors='replace')}"
        )
    payload = json.loads(body)
    for voice in payload.get("voices", []):
        if preferred_name.lower() in voice.get("name", "").lower():
            return (
                voice["voice_id"],
                voice["name"],
                f"preferred voice id {preferred_id} unavailable; resolved by name match",
            )
    raise RuntimeError(f"Preferred voice {preferred_name!r} could not be resolved.")


def synthesize_segment(
    *,
    api_key: str,
    voice_id: str,
    text: str,
    out_path: Path,
) -> None:
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.65,
            "similarity_boost": 0.75,
            "style": 0.10,
            "use_speaker_boost": True,
        },
    }
    status, body = eleven_request(
        "POST",
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + voice_id
        + "?output_format="
        + urllib.parse.quote("mp3_44100_128"),
        api_key,
        payload,
    )
    if status != 200:
        raise RuntimeError(
            f"ElevenLabs synthesis failed for text {text!r}: HTTP {status} {body.decode('utf-8', errors='replace')}"
        )
    out_path.write_bytes(body)


def sanitize_words(text: str) -> list[str]:
    return [word for word in text.upper().replace("-", " ").split() if word]


def measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0]), float(bbox[3] - bbox[1])


def pick_wrapped_lines(
    *,
    draw: ImageDraw.ImageDraw,
    words: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> tuple[list[list[str]], list[float], list[float]] | None:
    best_candidate = None
    for split_index in range(1, len(words)):
        candidate_lines = [words[:split_index], words[split_index:]]
        widths: list[float] = []
        heights: list[float] = []
        valid = True
        for line_words in candidate_lines:
            line_text = " ".join(line_words)
            width, height = measure_text(draw, line_text, font)
            if width > max_width:
                valid = False
                break
            widths.append(width)
            heights.append(height)
        if not valid:
            continue
        score = (max(widths), abs(widths[0] - widths[1]), abs(len(candidate_lines[0]) - len(candidate_lines[1])))
        if best_candidate is None or score < best_candidate["score"]:
            best_candidate = {
                "lines": candidate_lines,
                "widths": widths,
                "heights": heights,
                "score": score,
            }
    if best_candidate is None:
        return None
    return best_candidate["lines"], best_candidate["widths"], best_candidate["heights"]


def fit_caption(
    *,
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    max_width: int,
    max_font_size: int,
    min_font_size: int,
) -> CaptionLayout:
    words = sanitize_words(text)
    if not words:
        raise RuntimeError("Caption text is empty after sanitization.")

    one_line_text = " ".join(words)
    for font_size in range(max_font_size, min_font_size - 1, -2):
        font = ImageFont.truetype(str(font_path), font_size)
        one_line_width, one_line_height = measure_text(draw, one_line_text, font)
        if one_line_width <= max_width:
            return CaptionLayout(
                font=font,
                font_size=font_size,
                lines=[words],
                line_widths=[one_line_width],
                line_heights=[one_line_height],
                total_height=one_line_height,
            )

        wrapped = pick_wrapped_lines(draw=draw, words=words, font=font, max_width=max_width)
        if wrapped is not None:
            wrapped_lines, widths, heights = wrapped
            total_height = sum(heights) + CAPTION_LINE_GAP * (len(wrapped_lines) - 1)
            return CaptionLayout(
                font=font,
                font_size=font_size,
                lines=wrapped_lines,
                line_widths=widths,
                line_heights=heights,
                total_height=total_height,
            )

    raise RuntimeError(
        f"Caption could not fit within the {max_width}px safe zone at or above {min_font_size}px: {text!r}"
    )


def assert_bbox_within_safe_zone(bbox: tuple[int, int, int, int], label: str) -> None:
    left, top, right, bottom = bbox
    if left < SAFE_LEFT or right > SAFE_RIGHT or top < SAFE_TOP or bottom > SAFE_BOTTOM:
        raise RuntimeError(
            f"{label} escaped safe zone: bbox={bbox}, safe_zone=({SAFE_LEFT}, {SAFE_TOP}, {SAFE_RIGHT}, {SAFE_BOTTOM})"
        )


def build_background(seed: int, flash: bool = False) -> Image.Image:
    image = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), BACKGROUND)
    overlay = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for y in range(CANVAS_HEIGHT):
        blend = y / CANVAS_HEIGHT
        color = (
            int(10 + (18 - 10) * blend),
            int(10 + (16 - 10) * blend),
            int(15 + (30 - 15) * blend),
            255,
        )
        draw.line((0, y, CANVAS_WIDTH, y), fill=color)

    glow_alpha = 36 if flash else 20
    draw.ellipse((-180, -120, 560, 620), fill=(0, 229, 255, glow_alpha))
    draw.ellipse((480, 1060, 1260, 1900), fill=(0, 229, 255, glow_alpha + 8))
    draw.rectangle((0, 0, CANVAS_WIDTH, CANVAS_HEIGHT), outline=(255, 255, 255, 12), width=4)

    rng = random.Random(seed)
    for _ in range(320 if flash else 240):
        x = rng.randint(0, CANVAS_WIDTH - 1)
        y = rng.randint(0, CANVAS_HEIGHT - 1)
        alpha = rng.randint(8, 30 if flash else 24)
        overlay.putpixel((x, y), (255, 255, 255, alpha))

    if flash:
        draw.rounded_rectangle((70, 120, CANVAS_WIDTH - 70, CANVAS_HEIGHT - 120), radius=48, outline=(0, 229, 255, 80), width=4)
        draw.rectangle((0, 0, CANVAS_WIDTH, 28), fill=(0, 229, 255, 44))
        draw.rectangle((0, CANVAS_HEIGHT - 28, CANVAS_WIDTH, CANVAS_HEIGHT), fill=(0, 229, 255, 44))

    return Image.alpha_composite(image, overlay)


def draw_highlighted_lines(
    *,
    draw: ImageDraw.ImageDraw,
    lines: list[list[str]],
    font: ImageFont.FreeTypeFont,
    highlight_words: set[str],
    first_line_y: float,
    safe_left: int,
    safe_right: int,
    safe_top: int,
    safe_bottom: int,
) -> list[dict[str, Any]]:
    max_width = safe_right - safe_left
    metrics: list[dict[str, Any]] = []
    current_y = first_line_y
    space_width = draw.textlength(" ", font=font)
    for words in lines:
        line_text = " ".join(words)
        line_bbox = draw.textbbox((0, 0), line_text, font=font)
        line_width = line_bbox[2] - line_bbox[0]
        line_height = line_bbox[3] - line_bbox[1]
        line_left = safe_left + (max_width - line_width) / 2
        positioned_bbox = draw.textbbox((line_left, current_y), line_text, font=font)
        left, top, right, bottom = positioned_bbox
        if left < safe_left or right > safe_right or top < safe_top or bottom > safe_bottom:
            raise RuntimeError(
                f"caption line escaped safe zone: bbox={positioned_bbox}, safe_zone=({safe_left}, {safe_top}, {safe_right}, {safe_bottom})"
            )

        x = line_left
        for index, word in enumerate(words):
            color = CYAN if word in highlight_words else WHITE
            draw.text((x, current_y), word, font=font, fill=color)
            x += draw.textlength(word, font=font)
            if index < len(words) - 1:
                x += space_width

        metrics.append(
            {
                "text": line_text,
                "bbox": [int(value) for value in positioned_bbox],
                "x": float(line_left),
                "y": float(current_y),
            }
        )
        current_y += line_height + CAPTION_LINE_GAP
    return metrics


def render_body_slide(
    *,
    out_path: Path,
    caption: str,
    highlight: str,
    font_path: Path,
    seed: int,
    max_font_size: int,
    min_font_size: int,
    label_text: str,
) -> dict[str, Any]:
    image = build_background(seed)
    draw = ImageDraw.Draw(image)
    highlight_words = set(sanitize_words(highlight))
    label_font = ImageFont.truetype(str(font_path), 34)
    caption_layout = fit_caption(
        draw=draw,
        text=caption,
        font_path=font_path,
        max_width=SAFE_WIDTH,
        max_font_size=max_font_size,
        min_font_size=min_font_size,
    )

    caption_top = min(CAPTION_START_Y, SAFE_BOTTOM - caption_layout.total_height)
    if caption_top < SAFE_TOP:
        raise RuntimeError(
            f"Caption block does not fit vertically inside safe zone: top={caption_top}, total_height={caption_layout.total_height}"
        )

    card_top = max(SAFE_TOP, int(caption_top) - 86)
    card_bottom = min(SAFE_BOTTOM, int(caption_top + caption_layout.total_height) + 94)
    draw.rounded_rectangle(
        (SAFE_LEFT, card_top, SAFE_RIGHT, card_bottom),
        radius=36,
        fill=CARD,
        outline=(255, 255, 255, 24),
        width=3,
    )
    draw.rectangle((SAFE_LEFT + 36, card_top + 42, SAFE_LEFT + 292, card_top + 50), fill=CYAN)
    label_bbox = draw.textbbox((SAFE_LEFT + 36, card_top + 72), label_text, font=label_font)
    assert_bbox_within_safe_zone(label_bbox, "label")
    draw.text((SAFE_LEFT + 36, card_top + 72), label_text, font=label_font, fill=MUTED)

    line_metrics = draw_highlighted_lines(
        draw=draw,
        lines=caption_layout.lines,
        font=caption_layout.font,
        highlight_words=highlight_words,
        first_line_y=caption_top,
        safe_left=SAFE_LEFT,
        safe_right=SAFE_RIGHT,
        safe_top=SAFE_TOP,
        safe_bottom=SAFE_BOTTOM,
    )

    image.save(out_path)
    return {
        "font_size": caption_layout.font_size,
        "caption_top": float(caption_top),
        "caption_bottom": float(caption_top + caption_layout.total_height),
        "lines": line_metrics,
    }


def render_hook_slide(
    *,
    out_path: Path,
    caption: str,
    highlight: str,
    font_path: Path,
    seed: int,
) -> dict[str, Any]:
    image = build_background(seed, flash=True)
    draw = ImageDraw.Draw(image)
    highlight_words = set(sanitize_words(highlight))
    layout = fit_caption(
        draw=draw,
        text=caption,
        font_path=font_path,
        max_width=SAFE_WIDTH,
        max_font_size=HOOK_FONT_MAX,
        min_font_size=HOOK_FONT_MIN,
    )
    block_top = (CANVAS_HEIGHT - layout.total_height) / 2 - 60
    if block_top < SAFE_TOP:
        block_top = SAFE_TOP

    card_top = max(SAFE_TOP, int(block_top) - 90)
    card_bottom = min(SAFE_BOTTOM, int(block_top + layout.total_height) + 110)
    draw.rounded_rectangle(
        (SAFE_LEFT, card_top, SAFE_RIGHT, card_bottom),
        radius=44,
        fill=(16, 24, 36, 236),
        outline=(0, 229, 255, 90),
        width=4,
    )

    label_font = ImageFont.truetype(str(font_path), 30)
    label_text = "DAILYZ HOOK"
    label_bbox = draw.textbbox((SAFE_LEFT + 42, card_top + 42), label_text, font=label_font)
    assert_bbox_within_safe_zone(label_bbox, "hook label")
    draw.text((SAFE_LEFT + 42, card_top + 42), label_text, font=label_font, fill=CYAN)

    line_metrics = draw_highlighted_lines(
        draw=draw,
        lines=layout.lines,
        font=layout.font,
        highlight_words=highlight_words,
        first_line_y=block_top,
        safe_left=SAFE_LEFT,
        safe_right=SAFE_RIGHT,
        safe_top=SAFE_TOP,
        safe_bottom=SAFE_BOTTOM,
    )
    image.save(out_path)
    return {
        "font_size": layout.font_size,
        "style": "hook",
        "lines": line_metrics,
    }


def render_segment_slide(
    *,
    out_path: Path,
    caption: str,
    highlight: str,
    font_path: Path,
    seed: int,
    role: str,
) -> dict[str, Any]:
    if role == "hook":
        return render_hook_slide(out_path=out_path, caption=caption, highlight=highlight, font_path=font_path, seed=seed)
    if role == "cta":
        return render_body_slide(
            out_path=out_path,
            caption=caption,
            highlight=highlight,
            font_path=font_path,
            seed=seed,
            max_font_size=CTA_FONT_MAX,
            min_font_size=CTA_FONT_MIN,
            label_text="DAILYZ CTA",
        )
    return render_body_slide(
        out_path=out_path,
        caption=caption,
        highlight=highlight,
        font_path=font_path,
        seed=seed,
        max_font_size=BODY_FONT_MAX,
        min_font_size=BODY_FONT_MIN,
        label_text="DAILYZ",
    )


def render_clip(
    *,
    slide_path: Path,
    audio_path: Path,
    out_path: Path,
    spoken_duration: float,
    min_duration: float = 0.0,
) -> float:
    clip_duration = max(spoken_duration + SILENCE_GAP_SECONDS, min_duration)
    fade_out = max(clip_duration - 0.2, 0.1)
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(slide_path),
            "-i",
            str(audio_path),
            "-t",
            f"{clip_duration:.3f}",
            "-vf",
            f"fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.2,fade=t=out:st={fade_out:.3f}:d=0.2",
            "-af",
            f"apad=pad_dur={SILENCE_GAP_SECONDS:.1f},afade=t=in:st=0:d=0.2",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    return clip_duration


def concat_clips(clip_paths: list[Path], out_path: Path, temp_dir: Path) -> None:
    concat_path = temp_dir / "concat.txt"
    concat_path.write_text("".join(f"file '{clip.resolve()}'\n" for clip in clip_paths))
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            str(out_path),
        ]
    )


def decode_image_payload(payload: dict[str, Any]) -> Image.Image:
    image_items = payload.get("data") or []
    if not image_items:
        raise RuntimeError(f"OpenAI image response contained no data: {json.dumps(payload)[:500]}")
    item = image_items[0]
    if "b64_json" in item:
        return Image.open(io.BytesIO(base64.b64decode(item["b64_json"]))).convert("RGB")
    if "url" in item:
        with urllib.request.urlopen(item["url"], timeout=180) as response:
            return Image.open(io.BytesIO(response.read())).convert("RGB")
    raise RuntimeError(f"Unsupported image payload shape: {json.dumps(item)[:500]}")


def fit_cover_image(image: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = image.size
    src_ratio = src_w / src_h
    dst_ratio = width / height
    if src_ratio > dst_ratio:
        new_h = height
        new_w = int(height * src_ratio)
    else:
        new_w = width
        new_h = int(width / src_ratio)
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, (new_w - width) // 2)
    top = max(0, (new_h - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def render_thumbnail_text(
    *,
    image: Image.Image,
    text: str,
    highlight: str,
    font_path: Path,
) -> dict[str, Any]:
    canvas = fit_cover_image(image, THUMB_WIDTH, THUMB_HEIGHT).convert("RGBA")
    overlay = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw.rectangle((0, 0, THUMB_WIDTH, THUMB_HEIGHT), fill=(8, 10, 14, 54))
    for offset in range(420):
        alpha = int(180 * (offset / 420))
        draw.rectangle((0, 0, THUMB_WIDTH, THUMB_TEXT_TOP + 500 - offset), outline=None, fill=(10, 10, 15, min(alpha, 170)))
    draw.rounded_rectangle((THUMB_SAFE_LEFT - 20, THUMB_TEXT_TOP - 24, THUMB_SAFE_RIGHT + 20, 540), radius=36, fill=(10, 10, 15, 166))
    draw.rectangle((THUMB_SAFE_LEFT, THUMB_TEXT_TOP - 32, THUMB_SAFE_LEFT + 240, THUMB_TEXT_TOP - 20), fill=CYAN)

    layout = fit_caption(
        draw=draw,
        text=text,
        font_path=font_path,
        max_width=THUMB_SAFE_WIDTH,
        max_font_size=THUMB_FONT_MAX,
        min_font_size=THUMB_FONT_MIN,
    )
    highlight_words = set(sanitize_words(highlight))
    label_font = ImageFont.truetype(str(font_path), 28)
    draw.text((THUMB_SAFE_LEFT, THUMB_TEXT_TOP - 4), "DAILYZ", font=label_font, fill=(173, 182, 191, 255))

    current_y = THUMB_TEXT_TOP + 64
    line_metrics = []
    space_width = draw.textlength(" ", font=layout.font)
    for words in layout.lines:
        line_text = " ".join(words)
        bbox = draw.textbbox((0, 0), line_text, font=layout.font)
        line_width = bbox[2] - bbox[0]
        line_height = bbox[3] - bbox[1]
        line_left = THUMB_SAFE_LEFT + (THUMB_SAFE_WIDTH - line_width) / 2

        shadow_offsets = [(0, 5), (0, 0)]
        for shadow_x, shadow_y in shadow_offsets:
            x = line_left + shadow_x
            for index, word in enumerate(words):
                fill = (0, 0, 0, 120) if (shadow_x, shadow_y) != (0, 0) else (CYAN if word in highlight_words else WHITE)
                draw.text((x, current_y + shadow_y), word, font=layout.font, fill=fill)
                x += draw.textlength(word, font=layout.font)
                if index < len(words) - 1:
                    x += space_width

        line_metrics.append({"text": line_text, "y": float(current_y)})
        current_y += line_height + THUMB_TEXT_LINE_GAP

    combined = Image.alpha_composite(canvas, overlay).convert("RGB")
    return {"image": combined, "font_size": layout.font_size, "lines": line_metrics}


def generate_base_thumbnail_image(
    *,
    api_key: str,
    model: str,
    size: str,
    prompt: str,
) -> Image.Image:
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
    }
    status, body = openai_request("POST", "https://api.openai.com/v1/images/generations", api_key, payload)
    if status != 200:
        raise RuntimeError(f"OpenAI image generation failed: HTTP {status} {body.decode('utf-8', errors='replace')}")
    return decode_image_payload(json.loads(body))


def resolve_thumbnail_backend(
    *,
    api_key: str,
    topic: str,
) -> tuple[str, str, str]:
    prompt = (
        f"Vertical dark cinematic background for a short educational video about {topic}. "
        "Dark #0A0A0F background, cyan #00E5FF accents, dramatic subject lighting, no typography, no words, no logos, no watermark."
    )
    attempts = [
        ("dall-e-3", "1024x1792", "requested model"),
        ("gpt-image-1", "1024x1536", "fallback after DALL-E 3 rejection"),
    ]
    last_error = None
    for model, size, note in attempts:
        try:
            generate_base_thumbnail_image(api_key=api_key, model=model, size=size, prompt=prompt)
            return model, size, note
        except RuntimeError as error:
            last_error = str(error)
            if model == "dall-e-3" and "does not exist" in last_error:
                continue
            if model == "dall-e-3" and "not found" in last_error.lower():
                continue
            raise
    raise RuntimeError(last_error or "Could not resolve an OpenAI image model.")


def build_thumbnail_prompt(topic: str) -> str:
    return (
        f"Vertical cinematic background for a social video thumbnail about {topic}. "
        "Deep dark #0A0A0F scene, relevant central subject, crisp editorial composition, cyan #00E5FF accents, high contrast, bold lighting, no typography, no logo, no watermark."
    )


def render_thumbnail(
    *,
    video: dict[str, Any],
    out_path: Path,
    font_path: Path,
    api_key: str,
    image_model: str,
    image_size: str,
) -> dict[str, Any]:
    if out_path.exists():
        with Image.open(out_path) as existing:
            width, height = existing.size
        return {
            "topic": video["topic"],
            "hook_text": video["phrases"][0]["text"],
            "output_path": str(out_path),
            "image_model": "existing-file",
            "requested_size": "n/a",
            "final_size": [width, height],
            "reused_existing": True,
        }

    hook_phrase = video["phrases"][0]["text"]
    hook_highlight = " ".join(video["phrases"][0].get("keywords", []))
    prompt = build_thumbnail_prompt(video["topic"])
    base_image = generate_base_thumbnail_image(api_key=api_key, model=image_model, size=image_size, prompt=prompt)
    thumb = render_thumbnail_text(image=base_image, text=hook_phrase, highlight=hook_highlight, font_path=font_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    thumb["image"].save(out_path, format="JPEG", quality=92, optimize=True)
    return {
        "topic": video["topic"],
        "hook_text": hook_phrase,
        "output_path": str(out_path),
        "image_model": image_model,
        "requested_size": image_size,
        "final_size": [THUMB_WIDTH, THUMB_HEIGHT],
        "font_size": thumb["font_size"],
        "lines": thumb["lines"],
    }


def render_video(
    *,
    video: dict[str, Any],
    output_root: Path,
    publish_root: Path,
    voice_id: str,
    voice_name: str,
    voice_note: str,
    api_key: str,
    font_path: Path,
) -> dict[str, Any]:
    asset_dir = output_root / f"video-{video['video_id']}"
    audio_dir = asset_dir / "audio"
    slides_dir = asset_dir / "slides"
    clips_dir = asset_dir / "clips"
    temp_dir = asset_dir / "tmp"
    for directory in (audio_dir, slides_dir, clips_dir, temp_dir):
        directory.mkdir(parents=True, exist_ok=True)

    publish_dir = publish_root / video["publish_date"]
    publish_dir.mkdir(parents=True, exist_ok=True)
    final_path = publish_dir / f"{video['topic']}.mp4"

    if final_path.exists():
        return {
            "id": video["video_id"],
            "title": video["topic"],
            "publish_date": video["publish_date"],
            "duration_seconds": round(probe_duration(final_path), 3),
            "segment_count": len(video["phrases"]),
            "voice_name": voice_name,
            "voice_id": voice_id,
            "voice_resolution_note": voice_note,
            "output_path": str(final_path),
            "reused_existing": True,
            "segments": [],
        }

    clip_paths: list[Path] = []
    segment_reports = []
    last_index = len(video["phrases"]) - 1
    for index, phrase in enumerate(video["phrases"]):
        segment_number = index + 1
        role = "hook" if index == 0 else "cta" if index == last_index else "body"
        segment_id = f"{segment_number:02d}"
        audio_path = audio_dir / f"segment-{segment_id}.mp3"
        slide_path = slides_dir / f"slide-{segment_id}.png"
        clip_path = clips_dir / f"clip-{segment_id}.mp4"
        spoken_text = phrase["text"]
        highlight = " ".join(phrase.get("keywords", []))

        synthesize_segment(api_key=api_key, voice_id=voice_id, text=spoken_text, out_path=audio_path)
        slide_metrics = render_segment_slide(
            out_path=slide_path,
            caption=spoken_text,
            highlight=highlight,
            font_path=font_path,
            seed=int(video["video_id"]) * 100 + segment_number,
            role=role,
        )
        spoken_duration = probe_duration(audio_path)
        clip_duration = render_clip(
            slide_path=slide_path,
            audio_path=audio_path,
            out_path=clip_path,
            spoken_duration=spoken_duration,
            min_duration=HOOK_MIN_SECONDS if role == "hook" else 0.0,
        )
        clip_paths.append(clip_path)
        segment_reports.append(
            {
                "segment_id": segment_id,
                "role": role,
                "spoken": spoken_text,
                "highlight": highlight,
                "spoken_duration_seconds": round(spoken_duration, 3),
                "clip_duration_seconds": round(clip_duration, 3),
                "slide_path": str(slide_path),
                "audio_path": str(audio_path),
                "clip_path": str(clip_path),
                "slide_metrics": slide_metrics,
            }
        )

    concat_clips(clip_paths, final_path, temp_dir)
    duration = probe_duration(final_path)
    return {
        "id": video["video_id"],
        "title": video["topic"],
        "publish_date": video["publish_date"],
        "duration_seconds": round(duration, 3),
        "segment_count": len(video["phrases"]),
        "voice_name": voice_name,
        "voice_id": voice_id,
        "voice_resolution_note": voice_note,
        "output_path": str(final_path),
        "segments": segment_reports,
    }


def main() -> int:
    args = parse_args()
    scripts_path = Path(args.scripts)
    output_root = Path(args.output_root)
    publish_root = Path(args.publish_root)
    output_root.mkdir(parents=True, exist_ok=True)
    publish_root.mkdir(parents=True, exist_ok=True)

    elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is required.")

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required.")

    font_path = select_font_path()
    voice_id, voice_name, voice_note = resolve_voice(elevenlabs_api_key)
    image_model, image_size, image_model_note = resolve_thumbnail_backend(
        api_key=openai_api_key,
        topic="internet archive",
    )

    videos = load_scripts(scripts_path)
    selected_ids = set(args.only or [])
    if selected_ids:
        videos = [video for video in videos if video["video_id"] in selected_ids]
        if not videos:
            raise RuntimeError(f"No videos matched --only {sorted(selected_ids)}")

    video_reports = []
    thumbnail_reports = []
    for video in videos:
        video_reports.append(
            render_video(
                video=video,
                output_root=output_root,
                publish_root=publish_root,
                voice_id=voice_id,
                voice_name=voice_name,
                voice_note=voice_note,
                api_key=elevenlabs_api_key,
                font_path=font_path,
            )
        )
        thumbnail_reports.append(
            render_thumbnail(
                video=video,
                out_path=publish_root / video["publish_date"] / f"{video['topic']}.jpg",
                font_path=font_path,
                api_key=openai_api_key,
                image_model=image_model,
                image_size=image_size,
            )
        )

    report = {
        "voice": {
            "preset": "voice-chris-A",
            "voice_name": voice_name,
            "voice_id": voice_id,
            "voice_resolution_note": voice_note,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.65,
                "similarity_boost": 0.75,
                "style": 0.10,
                "speaker_boost": True,
            },
        },
        "image_backend": {
            "model": image_model,
            "requested_size": image_size,
            "note": image_model_note,
        },
        "publish_root": str(publish_root),
        "videos": video_reports,
        "thumbnails": thumbnail_reports,
    }
    (output_root / "render-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
