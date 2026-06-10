#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError
    from openai import APIStatusError, OpenAI
except ImportError as exc:  # pragma: no cover - exercised only in missing-dependency environments
    raise SystemExit(
        "Missing Python dependency. Install with: python -m pip install boto3 openai"
    ) from exc

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
R2_BUCKET_DEFAULT = "cigaler-assets"
IMAGE_PREFIX = "2 - Library/images/"
LOOP_PREFIX = "2 - Library/loops/"
IMAGE_INDEX_KEY = "3 - Production/asset_index/images_index.json"
LOOP_INDEX_KEY = "3 - Production/asset_index/loops_index.json"
LOOP_STYLE_SPECS_KEY = "3 - Production/creative_direction/loop_style_specs.md"
LOCAL_IMAGE_INDEX_PATH = Path("data/asset_index/images_index.json")
LOCAL_LOOP_INDEX_PATH = Path("data/asset_index/loops_index.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
LOOP_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
GPT_DELAY_SECONDS = 1.0

IMAGE_METADATA_PROMPT = """You are an asset librarian for a short-form video studio. Analyze this image and return ONLY valid JSON (no markdown, no code blocks) in this exact format:
{
  "description": "one detailed sentence describing the visual content",
  "best_for": ["topic1", "topic2", "topic3", "topic4", "topic5"],
  "avoid_for": ["topic1", "topic2"],
  "mood": "one or two words (e.g. futuristic, calm, dramatic, energetic)",
  "energy_level": "low | medium | high",
  "dominant_colors": ["#hex1", "#hex2"],
  "composition": "close-up | wide | abstract | aerial | portrait"
}"""

LOOP_METADATA_PROMPT_TEMPLATE = """You are an asset librarian for a short-form video studio. Based on the style name and variation number, provide metadata for this animated background loop. Return ONLY valid JSON (no markdown):
{
  "description": "one sentence describing the visual motion and feel",
  "best_for": ["topic1", "topic2", "topic3", "topic4", "topic5"],
  "avoid_for": ["topic1", "topic2"],
  "mood": "one or two words",
  "energy_level": "low | medium | high",
  "motion_type": "subtle | flowing | dynamic | pulsing | rotating",
  "color_palette": "warm | cool | neutral | colorful | monochrome"
}

Style: {style_name}
Variation: {variation_number}
Style description: {style_description}"""


@dataclass(frozen=True)
class AssetObject:
    key: str
    asset_id: str
    filename: str
    category: str


class AssetIndexUpdater:
    def __init__(self) -> None:
        self.bucket = os.getenv("R2_BUCKET", R2_BUCKET_DEFAULT)
        endpoint = os.getenv("R2_ENDPOINT_URL") or os.getenv("R2_ENDPOINT")
        access_key = os.getenv("R2_ACCESS_KEY_ID")
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        missing = [
            name
            for name, value in (
                ("R2_ENDPOINT_URL or R2_ENDPOINT", endpoint),
                ("R2_ACCESS_KEY_ID", access_key),
                ("R2_SECRET_ACCESS_KEY", secret_key),
                ("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY")),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.ai_disabled_reason: str | None = None

    def update_images(self, rebuild: bool = False) -> dict[str, Any]:
        objects = self._list_assets(IMAGE_PREFIX, IMAGE_EXTENSIONS)
        existing = self._load_index(IMAGE_INDEX_KEY, rebuild=rebuild)
        existing_assets = {} if rebuild else self._current_existing_assets(existing, objects)
        indexed_paths = {asset.get("path") for asset in existing_assets.values()}
        new_objects = [asset for asset in objects if asset.key not in indexed_paths]

        for index, asset in enumerate(new_objects, start=1):
            print(f"[images] indexing {index}/{len(new_objects)} {asset.key}", flush=True)
            metadata = self._generate_image_metadata(asset)
            existing_assets[asset.asset_id] = {
                "path": asset.key,
                "category": asset.category,
                "filename": asset.filename,
                **metadata,
            }

        payload = self._build_index_payload(existing_assets)
        self._save_index(IMAGE_INDEX_KEY, LOCAL_IMAGE_INDEX_PATH, payload)
        return payload

    def update_loops(self, rebuild: bool = False) -> dict[str, Any]:
        objects = self._list_assets(LOOP_PREFIX, LOOP_EXTENSIONS)
        existing = self._load_index(LOOP_INDEX_KEY, rebuild=rebuild)
        existing_assets = {} if rebuild else self._current_existing_assets(existing, objects)
        indexed_paths = {asset.get("path") for asset in existing_assets.values()}
        new_objects = [asset for asset in objects if asset.key not in indexed_paths]
        style_specs = self._load_loop_style_specs()

        for index, asset in enumerate(new_objects, start=1):
            style_name, variation_number = self._loop_style_and_variation(asset)
            style_description = self._style_description(style_specs, style_name)
            print(f"[loops] indexing {index}/{len(new_objects)} {asset.key}", flush=True)
            metadata = self._generate_loop_metadata(style_name, variation_number, style_description)
            existing_assets[asset.asset_id] = {
                "path": asset.key,
                "category": asset.category,
                "style": style_name,
                "variation": variation_number,
                "filename": asset.filename,
                **metadata,
            }

        payload = self._build_index_payload(existing_assets)
        self._save_index(LOOP_INDEX_KEY, LOCAL_LOOP_INDEX_PATH, payload)
        return payload

    def _list_assets(self, prefix: str, extensions: set[str]) -> list[AssetObject]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                if key and not key.endswith("/") and PurePosixPath(key).suffix.lower() in extensions:
                    keys.append(key)

        taken_ids: set[str] = set()
        assets: list[AssetObject] = []
        for key in sorted(keys):
            path = PurePosixPath(key)
            filename = path.name
            category = self._category_from_key(prefix, key)
            asset_id = self._unique_asset_id(path.stem, category, taken_ids)
            taken_ids.add(asset_id)
            assets.append(AssetObject(key=key, asset_id=asset_id, filename=filename, category=category))
        return assets

    def _category_from_key(self, prefix: str, key: str) -> str:
        relative = key.removeprefix(prefix)
        parts = PurePosixPath(relative).parts
        if len(parts) > 1:
            return self._slug(parts[0])
        stem = PurePosixPath(key).stem
        match = re.match(r"([a-zA-Z]+(?:[_-][a-zA-Z]+)*)[_-]?\d+", stem)
        return self._slug(match.group(1)) if match else "uncategorized"

    def _unique_asset_id(self, stem: str, category: str, taken_ids: set[str]) -> str:
        base = self._slug(stem)
        if base not in taken_ids:
            return base
        category_base = self._slug(f"{category}_{stem}")
        if category_base not in taken_ids:
            return category_base
        suffix = 2
        while f"{category_base}_{suffix}" in taken_ids:
            suffix += 1
        return f"{category_base}_{suffix}"

    def _load_index(self, key: str, rebuild: bool) -> dict[str, Any]:
        if rebuild:
            return self._empty_index()
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            payload = json.loads(body.decode("utf-8"))
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code not in {"NoSuchKey", "404", "NotFound"}:
                raise
            payload = self._empty_index()
        if not isinstance(payload, dict) or not isinstance(payload.get("assets"), dict):
            return self._empty_index()
        return payload

    def _current_existing_assets(self, existing: dict[str, Any], current_objects: list[AssetObject]) -> dict[str, Any]:
        current_paths = {asset.key for asset in current_objects}
        return {
            asset_id: asset
            for asset_id, asset in existing.get("assets", {}).items()
            if isinstance(asset, dict) and asset.get("path") in current_paths
        }

    def _build_index_payload(self, assets: dict[str, Any]) -> dict[str, Any]:
        ordered_assets = {asset_id: assets[asset_id] for asset_id in sorted(assets)}
        return {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "total_assets": len(ordered_assets),
            "assets": ordered_assets,
        }

    def _save_index(self, r2_key: str, local_path: Path, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=r2_key, Body=encoded, ContentType="application/json")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(encoded + b"\n")
        print(f"saved {payload['total_assets']} assets -> {r2_key} and {local_path}", flush=True)

    def _generate_image_metadata(self, asset: AssetObject) -> dict[str, Any]:
        if self.ai_disabled_reason:
            return self._fallback_image_metadata(asset, self.ai_disabled_reason)
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=asset.key)["Body"].read()
            mime_type = mimetypes.guess_type(asset.filename)[0] or "image/jpeg"
            image_url = f"data:{mime_type};base64,{base64.b64encode(body).decode('ascii')}"
            response = self.client.responses.create(
                model=OPENAI_MODEL,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": IMAGE_METADATA_PROMPT},
                            {"type": "input_image", "image_url": image_url},
                        ],
                    }
                ],
            )
            time.sleep(GPT_DELAY_SECONDS)
            return self._normalize_image_metadata(self._parse_json(response.output_text))
        except Exception as exc:  # noqa: BLE001 - fallback metadata is required for per-asset failures
            if self._is_terminal_openai_error(exc):
                self.ai_disabled_reason = f"OpenAI disabled after {type(exc).__name__}: {exc}"
            reason = self.ai_disabled_reason or f"vision analysis failed: {type(exc).__name__}: {exc}"
            print(f"[images] fallback for {asset.key}: {reason}", file=sys.stderr, flush=True)
            return self._fallback_image_metadata(asset, reason)

    def _generate_loop_metadata(
        self, style_name: str, variation_number: str, style_description: str
    ) -> dict[str, Any]:
        if self.ai_disabled_reason:
            return self._fallback_loop_metadata(style_name, variation_number, self.ai_disabled_reason)
        prompt = (
            LOOP_METADATA_PROMPT_TEMPLATE.replace("{style_name}", style_name)
            .replace("{variation_number}", variation_number)
            .replace("{style_description}", style_description or "No style description available.")
        )
        try:
            response = self.client.responses.create(
                model=OPENAI_MODEL,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            )
            time.sleep(GPT_DELAY_SECONDS)
            return self._normalize_loop_metadata(self._parse_json(response.output_text))
        except Exception as exc:  # noqa: BLE001 - fallback metadata is required for per-asset failures
            if self._is_terminal_openai_error(exc):
                self.ai_disabled_reason = f"OpenAI disabled after {type(exc).__name__}: {exc}"
            reason = self.ai_disabled_reason or f"loop metadata failed: {type(exc).__name__}: {exc}"
            print(f"[loops] fallback for {style_name} {variation_number}: {reason}", file=sys.stderr, flush=True)
            return self._fallback_loop_metadata(style_name, variation_number, reason)

    def _is_terminal_openai_error(self, exc: Exception) -> bool:
        if isinstance(exc, APIStatusError):
            return exc.status_code in {401, 403, 429}
        text = str(exc).lower()
        return any(marker in text for marker in ("quota", "billing", "rate limit", "too many requests"))

    def _parse_json(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            loaded = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            loaded = json.loads(cleaned[start : end + 1])
        if not isinstance(loaded, dict):
            raise ValueError("metadata response was not a JSON object")
        return loaded

    def _normalize_image_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "description": self._string(metadata.get("description"), "Unspecified image background."),
            "best_for": self._string_list(metadata.get("best_for"), fallback=["general", "background", "short-form video"]),
            "avoid_for": self._string_list(metadata.get("avoid_for"), fallback=["unrelated topics"]),
            "mood": self._string(metadata.get("mood"), "neutral"),
            "energy_level": self._enum(metadata.get("energy_level"), {"low", "medium", "high"}, "medium"),
            "dominant_colors": self._colors(metadata.get("dominant_colors")),
            "composition": self._enum(
                metadata.get("composition"), {"close-up", "wide", "abstract", "aerial", "portrait"}, "portrait"
            ),
        }

    def _normalize_loop_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "description": self._string(metadata.get("description"), "Animated background loop."),
            "best_for": self._string_list(metadata.get("best_for"), fallback=["general", "background", "short-form video"]),
            "avoid_for": self._string_list(metadata.get("avoid_for"), fallback=["static visuals"]),
            "mood": self._string(metadata.get("mood"), "neutral"),
            "energy_level": self._enum(metadata.get("energy_level"), {"low", "medium", "high"}, "medium"),
            "motion_type": self._enum(
                metadata.get("motion_type"), {"subtle", "flowing", "dynamic", "pulsing", "rotating"}, "subtle"
            ),
            "color_palette": self._enum(
                metadata.get("color_palette"), {"warm", "cool", "neutral", "colorful", "monochrome"}, "cool"
            ),
        }

    def _fallback_image_metadata(self, asset: AssetObject, reason: str) -> dict[str, Any]:
        topic = asset.category.replace("_", " ")
        return self._normalize_image_metadata(
            {
                "description": f"A {topic} themed short-form video background inferred from {asset.filename}; generated as fallback because {reason}.",
                "best_for": [topic, asset.category, "short-form video", "background visuals", "evergreen content"],
                "avoid_for": ["unrelated niches", "literal product demos"],
                "mood": "cinematic",
                "energy_level": "medium",
                "dominant_colors": ["#0A0A0F", "#00E5FF"],
                "composition": "portrait",
            }
        )

    def _fallback_loop_metadata(self, style_name: str, variation_number: str, reason: str) -> dict[str, Any]:
        return self._normalize_loop_metadata(
            {
                "description": f"A {style_name} animated background loop, variation {variation_number}, inferred from filename because {reason}.",
                "best_for": [style_name, "ambient background", "short-form video", "technology", "evergreen narration"],
                "avoid_for": ["still image sequences", "overly literal scenes"],
                "mood": "ambient",
                "energy_level": "medium",
                "motion_type": self._fallback_motion_type(style_name),
                "color_palette": "cool",
            }
        )

    def _fallback_motion_type(self, style_name: str) -> str:
        style = style_name.lower()
        if any(token in style for token in ("bokeh", "particles", "light")):
            return "pulsing"
        if any(token in style for token in ("smoke", "gradient", "nebula")):
            return "flowing"
        if any(token in style for token in ("geometric", "neural", "lines")):
            return "dynamic"
        return "subtle"

    def _loop_style_and_variation(self, asset: AssetObject) -> tuple[str, str]:
        stem = PurePosixPath(asset.filename).stem
        variation_match = re.search(r"(?:variation|var|v)?[_-]?(\d+)$", stem, flags=re.IGNORECASE)
        variation = variation_match.group(1) if variation_match else "unknown"
        if asset.category != "uncategorized":
            style = asset.category
        elif variation_match:
            style = stem[: variation_match.start()].strip("_- ") or stem
        else:
            style = stem
        return self._slug(style), variation

    def _load_loop_style_specs(self) -> dict[str, str]:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=LOOP_STYLE_SPECS_KEY)["Body"].read()
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code not in {"NoSuchKey", "404", "NotFound"}:
                raise
            return {}
        return self._parse_style_specs(body.decode("utf-8", errors="replace"))

    def _parse_style_specs(self, markdown: str) -> dict[str, str]:
        specs: dict[str, list[str]] = {}
        current: str | None = None
        for line in markdown.splitlines():
            heading = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
            if heading:
                title = re.sub(r"^[\d.\-\s]+", "", heading.group(1)).strip()
                title = re.sub(r"\s*[:—-].*$", "", title).strip()
                current = self._slug(title)
                specs.setdefault(current, [])
                continue
            if current and line.strip():
                specs[current].append(line.strip())
        return {key: " ".join(value)[:1200] for key, value in specs.items() if value}

    def _style_description(self, specs: dict[str, str], style_name: str) -> str:
        slug = self._slug(style_name)
        if slug in specs:
            return specs[slug]
        for key, description in specs.items():
            if slug in key or key in slug:
                return description
        return ""

    def _string(self, value: Any, fallback: str) -> str:
        return str(value).strip() if value is not None and str(value).strip() else fallback

    def _string_list(self, value: Any, fallback: list[str]) -> list[str]:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
        elif isinstance(value, str) and value.strip():
            items = [part.strip() for part in value.split(",") if part.strip()]
        else:
            items = []
        return items[:5] if items else fallback

    def _colors(self, value: Any) -> list[str]:
        colors = self._string_list(value, ["#0A0A0F", "#00E5FF"])
        normalized = []
        for color in colors[:2]:
            match = re.search(r"#?[0-9a-fA-F]{6}", color)
            normalized.append(("#" + match.group(0).lstrip("#")).upper() if match else color)
        return normalized or ["#0A0A0F", "#00E5FF"]

    def _enum(self, value: Any, allowed: set[str], fallback: str) -> str:
        normalized = str(value).strip().lower() if value is not None else ""
        return normalized if normalized in allowed else fallback

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
        return slug or "uncategorized"

    def _empty_index(self) -> dict[str, Any]:
        return {"generated_at": None, "total_assets": 0, "assets": {}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or incrementally update DailyZ R2 asset indexes.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--images", action="store_true", help="Scan images and add new entries only.")
    group.add_argument("--loops", action="store_true", help="Scan loops and add new entries only.")
    group.add_argument("--all", action="store_true", help="Scan images and loops.")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild selected index(es) from scratch.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not (args.images or args.loops or args.all or args.rebuild):
        parser.error("choose --images, --loops, --all, or --rebuild")

    rebuild_all = args.rebuild and not (args.images or args.loops or args.all)
    run_images = args.images or args.all or rebuild_all
    run_loops = args.loops or args.all or rebuild_all
    updater = AssetIndexUpdater()
    results: dict[str, int] = {}

    if run_images:
        results["images"] = updater.update_images(rebuild=args.rebuild)["total_assets"]
    if run_loops:
        results["loops"] = updater.update_loops(rebuild=args.rebuild)["total_assets"]

    print(json.dumps({"updated": results, "rebuild": args.rebuild}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
