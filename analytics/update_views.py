#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


VALID_PLATFORMS = {"youtube", "tiktok"}
VALID_TIMEFRAMES = {"24h", "7d", "30d"}
FLAT_THRESHOLD = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update a view count in analytics/performance_log.json."
    )
    parser.add_argument("video_id", help="Zero-padded DailyZ video id, for example 005")
    parser.add_argument("platform", choices=sorted(VALID_PLATFORMS))
    parser.add_argument("timeframe", choices=sorted(VALID_TIMEFRAMES))
    parser.add_argument("view_count", type=int, help="Non-negative integer view count")
    return parser.parse_args()


def load_log(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing performance log: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in performance log: {exc}") from exc

    if not isinstance(data, list):
        raise SystemExit("performance_log.json must contain a top-level JSON array.")
    return data


def save_log(path: Path, data: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def find_video(data: list[dict], video_id: str) -> dict:
    for entry in data:
        if entry.get("video_id") == video_id:
            return entry
    raise SystemExit(f"Video id not found: {video_id}")


def numeric_values(
    data: list[dict], platform: str, timeframe: str
) -> list[tuple[int, int]]:
    values: list[tuple[int, int]] = []
    for entry in data:
        video_id = entry.get("video_id")
        if not isinstance(video_id, str) or not video_id.isdigit():
            continue

        value = (
            entry.get("views", {})
            .get(platform, {})
            .get(timeframe)
        )
        if isinstance(value, int):
            values.append((int(video_id), value))

    values.sort(key=lambda item: item[0])
    return values


def average(items: list[int]) -> float:
    return sum(items) / len(items)


def trend_direction(values: list[int]) -> tuple[str, float | None, float | None]:
    if len(values) < 10:
        return "flat", None, None

    previous_batch = values[-10:-5]
    current_batch = values[-5:]
    previous_average = average(previous_batch)
    current_average = average(current_batch)

    if previous_average == 0:
        if current_average == 0:
            return "flat", previous_average, current_average
        return "up", previous_average, current_average

    delta = (current_average - previous_average) / previous_average
    if delta > FLAT_THRESHOLD:
        return "up", previous_average, current_average
    if delta < -FLAT_THRESHOLD:
        return "down", previous_average, current_average
    return "flat", previous_average, current_average


def main() -> int:
    args = parse_args()
    if args.view_count < 0:
        raise SystemExit("view_count must be a non-negative integer.")

    log_path = Path(__file__).with_name("performance_log.json")
    data = load_log(log_path)
    video = find_video(data, args.video_id)
    video.setdefault("views", {}).setdefault(args.platform, {})[args.timeframe] = args.view_count
    save_log(log_path, data)

    platform_values = numeric_values(data, args.platform, args.timeframe)
    raw_values = [value for _, value in platform_values]
    current_average = average(raw_values)
    direction, previous_average, batch_average = trend_direction(raw_values)

    print(
        f"Updated {args.video_id} {args.platform} {args.timeframe} views to {args.view_count}."
    )
    print(
        f"Current average views ({args.platform} {args.timeframe}): {current_average:.2f}"
    )
    if previous_average is None or batch_average is None:
        print("Trend vs previous batch of 5: flat (need 10 populated data points).")
    else:
        print(
            "Trend vs previous batch of 5: "
            f"{direction} (previous avg: {previous_average:.2f}, "
            f"current avg: {batch_average:.2f})"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
