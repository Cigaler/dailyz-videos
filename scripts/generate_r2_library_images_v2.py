#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, OpenAI, RateLimitError
from PIL import Image, ImageOps

R2_PREFIX = "2 - Library/images"
DEFAULT_PROMPTS_URL = "https://raw.githubusercontent.com/Cigaler/dailyz-videos/main/data/r2_library_images/prompts_v2.json"
DEFAULT_PRICE_PER_IMAGE_USD = 0.04
IMAGE_SIZE = "1024x1792"
IMAGE_QUALITY = "high"
OUTPUT_SIZE = (1080, 1920)
PROMPT_SUFFIX = (
    "Vertical 9:16 portrait composition for a short-form video background, "
    "cinematic lighting, high detail, no text, no captions, no logo, no watermark."
)
CATEGORY_LABELS = {
    "tech_ai": "Technology / AI",
    "finance": "Finance / Business",
    "motivation": "Motivation / Mindset",
    "history": "History",
    "science": "Science",
    "space": "Space / Astronomy",
    "nature": "Nature",
    "psychology": "Psychology",
    "mysteries": "Mysteries / Amazing Facts",
    "geography": "Geography / Countries",
    "luxury": "Luxury / Wealth",
    "futurism": "Futurism / Cyberpunk",
    "health": "Health / Wellness",
    "productivity": "Productivity",
    "abstract": "Abstract / Universal",
}
CATEGORY_ORDER = tuple(CATEGORY_LABELS)
TERMINAL_ERROR_MARKERS = (
    "insufficient credits",
    "quota exceeded",
    "upgrade your plan",
    "too many requests",
    "rate limit",
    "payment required",
    "billing required",
    "unauthorized",
    "forbidden",
    "permission denied",
    "not authenticated",
    "invalid api key",
    "incorrect api key",
)
RETRYABLE_GENERATION_STATUS_CODES = {429, 502}


@dataclass(frozen=True)
class ImageJob:
    filename: str
    category_label: str
    category_slug: str
    image_number: int
    base_prompt: str
    generation_prompt: str
    r2_key: str


@dataclass(frozen=True)
class GeneratedImage:
    job: ImageJob
    image_bytes: bytes
    revised_prompt: str | None
    elapsed_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate DailyZ v2 library images and upload them to Cloudflare R2."
    )
    parser.add_argument("--prompts-path", type=Path, default=Path("data/r2_library_images/prompts_v2.json"))
    parser.add_argument("--prompts-url", default=DEFAULT_PROMPTS_URL)
    parser.add_argument("--refresh-prompts", action="store_true")
    parser.add_argument("--manifest-path", type=Path, default=Path("data/r2_library_images/manifest.jsonl"))
    parser.add_argument("--failure-log-path", type=Path, default=Path("data/r2_library_images/failures.jsonl"))
    parser.add_argument("--summary-path", type=Path, default=Path("data/r2_library_images/summary.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("output/generated/r2_library_images"))
    parser.add_argument("--model", default=os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"))
    parser.add_argument("--size", default=os.getenv("OPENAI_IMAGE_SIZE", IMAGE_SIZE))
    parser.add_argument("--quality", default=os.getenv("OPENAI_IMAGE_QUALITY", IMAGE_QUALITY))
    parser.add_argument("--category", action="append", dest="categories", default=[])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--submit-interval-seconds",
        type=float,
        default=float(os.getenv("OPENAI_IMAGE_SUBMIT_INTERVAL_SECONDS", "15")),
        help="Minimum delay between OpenAI image generation submissions. Defaults to 15s (4/min).",
    )
    parser.add_argument("--max-images", type=int, default=None)
    parser.add_argument("--max-runtime-seconds", type=float, default=None)
    parser.add_argument("--skip-existing-r2-check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def count_jsonl_records(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def load_successful_keys(manifest_path: Path) -> set[str]:
    return {
        record["r2_key"]
        for record in load_jsonl_records(manifest_path)
        if record.get("status") == "uploaded" and record.get("r2_key")
    }


def refresh_prompt_file(prompts_url: str, prompts_path: Path) -> None:
    response = requests.get(prompts_url, timeout=60)
    response.raise_for_status()
    prompts_path.parent.mkdir(parents=True, exist_ok=True)
    prompts_path.write_bytes(response.content)


def load_prompt_data(prompts_path: Path) -> dict[str, Any]:
    try:
        prompt_data = json.loads(prompts_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing prompts file: {prompts_path}") from exc
    if not isinstance(prompt_data, dict) or not isinstance(prompt_data.get("categories"), dict):
        raise SystemExit("prompts_v2.json must contain a categories object")
    return prompt_data


def build_jobs(prompt_data: dict[str, Any], selected_categories: set[str] | None) -> list[ImageJob]:
    jobs: list[ImageJob] = []
    categories = prompt_data["categories"]
    for category_slug in CATEGORY_ORDER:
        if selected_categories and category_slug not in selected_categories:
            continue
        prompt_items = categories.get(category_slug)
        if not isinstance(prompt_items, list):
            raise SystemExit(f"Missing prompt list for category: {category_slug}")
        for prompt_item in prompt_items:
            if not isinstance(prompt_item, dict):
                raise SystemExit(f"Invalid prompt item in category: {category_slug}")
            prompt_id = str(prompt_item.get("id", ""))
            base_prompt = str(prompt_item.get("prompt", "")).strip()
            if not prompt_id or not base_prompt:
                raise SystemExit(f"Prompt item missing id or prompt in category: {category_slug}")
            filename = f"{prompt_id}.jpg"
            expected_prefix = f"{category_slug}_"
            if not prompt_id.startswith(expected_prefix):
                raise SystemExit(f"Unexpected prompt id {prompt_id!r} for category {category_slug}")
            try:
                image_number = int(prompt_id.removeprefix(expected_prefix))
            except ValueError as exc:
                raise SystemExit(f"Prompt id is not zero-padded numeric: {prompt_id}") from exc
            generation_prompt = f"{base_prompt} {PROMPT_SUFFIX}"
            jobs.append(
                ImageJob(
                    filename=filename,
                    category_label=CATEGORY_LABELS[category_slug],
                    category_slug=category_slug,
                    image_number=image_number,
                    base_prompt=base_prompt,
                    generation_prompt=generation_prompt,
                    r2_key=f"{R2_PREFIX}/{category_slug}/{filename}",
                )
            )
    return jobs


def build_s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=ensure_env("R2_ENDPOINT_URL"),
        aws_access_key_id=ensure_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=ensure_env("R2_SECRET_ACCESS_KEY"),
    )


def list_existing_r2_keys(s3_client: Any, bucket: str, selected_categories: set[str] | None) -> set[str]:
    prefixes = (
        [f"{R2_PREFIX}/{category_slug}/" for category_slug in sorted(selected_categories)]
        if selected_categories
        else [f"{R2_PREFIX}/"]
    )
    existing_keys: set[str] = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key")
                if key:
                    existing_keys.add(key)
    return existing_keys


def get_image_bytes(image_data: Any) -> bytes:
    b64_json = getattr(image_data, "b64_json", None)
    if b64_json:
        return base64.b64decode(b64_json)
    url = getattr(image_data, "url", None)
    if url:
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        return response.content
    raise RuntimeError("Image generation response did not include b64_json or url.")


def retryable_generation_error(exc: BaseException) -> bool:
    if isinstance(exc, APITimeoutError):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code in RETRYABLE_GENERATION_STATUS_CODES:
        return True
    text = str(exc).lower()
    return "error code: 502" in text or "error code: 429" in text or "timed out" in text


def generate_image(job: ImageJob, model: str, size: str, quality: str) -> GeneratedImage:
    started_at = time.monotonic()
    client = OpenAI(api_key=ensure_env("OPENAI_API_KEY"), timeout=180, max_retries=0)
    for attempt in range(2):
        try:
            response = client.images.generate(
                model=model,
                prompt=job.generation_prompt,
                size=size,
                quality=quality,
                output_format="jpeg",
                n=1,
            )
            break
        except (RateLimitError, APIStatusError, APITimeoutError) as exc:
            if attempt == 0 and retryable_generation_error(exc):
                print(
                    f"RETRY in 60s after {getattr(exc, 'status_code', 'unknown')} for "
                    f"{job.category_slug}/{job.filename}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(60)
                continue
            raise
    image_data = response.data[0]
    return GeneratedImage(
        job=job,
        image_bytes=get_image_bytes(image_data),
        revised_prompt=getattr(image_data, "revised_prompt", None),
        elapsed_seconds=time.monotonic() - started_at,
    )


def save_as_jpeg(image_bytes: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(image_bytes)) as image:
        ImageOps.fit(
            image.convert("RGB"),
            OUTPUT_SIZE,
            method=Image.Resampling.LANCZOS,
        ).save(destination, format="JPEG", quality=95, optimize=True)


def upload_image(s3_client: Any, bucket: str, local_path: Path, r2_key: str) -> None:
    s3_client.upload_file(str(local_path), bucket, r2_key, ExtraArgs={"ContentType": "image/jpeg"})


def terminal_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {401, 403}:
        return True
    text = str(exc).lower()
    if status_code == 429:
        terminal_429_markers = (
            "insufficient credits",
            "quota exceeded",
            "upgrade your plan",
            "payment required",
            "billing required",
        )
        return any(marker in text for marker in terminal_429_markers)
    return any(marker in text for marker in TERMINAL_ERROR_MARKERS)


def selected_category_slugs(jobs: list[ImageJob]) -> list[str]:
    present = {job.category_slug for job in jobs}
    return [slug for slug in CATEGORY_ORDER if slug in present]


def uploaded_counts(jobs: list[ImageJob], successful_keys: set[str]) -> dict[str, int]:
    counts = {category_slug: 0 for category_slug in selected_category_slugs(jobs)}
    for job in jobs:
        if job.r2_key in successful_keys:
            counts[job.category_slug] = counts.get(job.category_slug, 0) + 1
    return counts


def next_missing_filename(jobs: list[ImageJob], successful_keys: set[str]) -> str | None:
    for job in jobs:
        if job.r2_key not in successful_keys:
            return job.filename
    return None


def write_summary(
    summary_path: Path,
    failure_log_path: Path,
    jobs: list[ImageJob],
    successful_keys: set[str],
    model: str,
    size: str,
    quality: str,
    submit_interval_seconds: float,
    attempted: int,
    uploaded: int,
    skipped_manifest: int,
    skipped_existing_r2: int,
    terminal_error_message: str | None = None,
) -> None:
    counts = uploaded_counts(jobs, successful_keys)
    complete_categories = [slug for slug, count in counts.items() if count >= 20]
    partial_categories = {slug: count for slug, count in counts.items() if 0 < count < 20}
    missing_by_category = {slug: max(0, 20 - count) for slug, count in counts.items() if count < 20}
    summary = {
        "updated_at": utc_now(),
        "prompt_version": "v2",
        "model": model,
        "size": size,
        "quality": quality,
        "submit_interval_seconds": submit_interval_seconds,
        "output_size": f"{OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]}",
        "planned_total": len(jobs),
        "attempted_this_run": attempted,
        "uploaded_this_run": uploaded,
        "skipped_manifest": skipped_manifest,
        "skipped_existing_r2": skipped_existing_r2,
        "estimated_spend_usd_this_run": round(uploaded * DEFAULT_PRICE_PER_IMAGE_USD, 2),
        "uploaded_total": sum(counts.values()),
        "uploaded_by_category": counts,
        "complete_categories": complete_categories,
        "partial_categories": partial_categories,
        "missing_by_category": missing_by_category,
        "failure_total": count_jsonl_records(failure_log_path),
        "estimated_spend_usd_using_task_assumption": round(
            sum(counts.values()) * DEFAULT_PRICE_PER_IMAGE_USD,
            2,
        ),
        "next_filename": next_missing_filename(jobs, successful_keys),
    }
    if terminal_error_message:
        summary["terminal_error"] = terminal_error_message
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def record_r2_preexisting(job: ImageJob, manifest_path: Path, model: str) -> None:
    append_jsonl(
        manifest_path,
        {
            "timestamp": utc_now(),
            "status": "uploaded",
            "source": "r2-preexisting",
            "prompt_version": "v2",
            "filename": job.filename,
            "category": job.category_label,
            "category_slug": job.category_slug,
            "image_number": job.image_number,
            "model": model,
            "base_prompt": job.base_prompt,
            "generation_prompt": job.generation_prompt,
            "r2_key": job.r2_key,
            "local_path": None,
        },
    )


def record_upload(result: GeneratedImage, local_path: Path, manifest_path: Path, model: str, size: str, quality: str) -> None:
    append_jsonl(
        manifest_path,
        {
            "timestamp": utc_now(),
            "status": "uploaded",
            "source": "generated",
            "prompt_version": "v2",
            "filename": result.job.filename,
            "category": result.job.category_label,
            "category_slug": result.job.category_slug,
            "image_number": result.job.image_number,
            "model": model,
            "size": size,
            "quality": quality,
            "output_size": f"{OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]}",
            "base_prompt": result.job.base_prompt,
            "generation_prompt": result.job.generation_prompt,
            "revised_prompt": result.revised_prompt,
            "r2_key": result.job.r2_key,
            "local_path": str(local_path),
            "generation_elapsed_seconds": round(result.elapsed_seconds, 3),
        },
    )


def record_failure(job: ImageJob, failure_log_path: Path, model: str, size: str, quality: str, exc: BaseException) -> None:
    append_jsonl(
        failure_log_path,
        {
            "timestamp": utc_now(),
            "filename": job.filename,
            "category": job.category_label,
            "category_slug": job.category_slug,
            "prompt_version": "v2",
            "model": model,
            "size": size,
            "quality": quality,
            "output_size": f"{OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]}",
            "base_prompt": job.base_prompt,
            "generation_prompt": job.generation_prompt,
            "r2_key": job.r2_key,
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
    )


def submit_one_if_ready(
    executor: ThreadPoolExecutor,
    futures: dict[Future[GeneratedImage], ImageJob],
    pending_jobs: list[ImageJob],
    args: argparse.Namespace,
    deadline_at: float | None,
    submitted_count: int,
    next_submit_at: float,
) -> tuple[int, float, bool]:
    if not pending_jobs or len(futures) >= args.workers:
        return submitted_count, next_submit_at, False
    if args.max_images is not None and submitted_count >= args.max_images:
        return submitted_count, next_submit_at, False
    now = time.monotonic()
    if deadline_at is not None and now >= deadline_at:
        return submitted_count, next_submit_at, False
    if now < next_submit_at:
        return submitted_count, next_submit_at, False

    job = pending_jobs.pop(0)
    future = executor.submit(generate_image, job, args.model, args.size, args.quality)
    futures[future] = job
    submitted_count += 1
    next_submit_at = time.monotonic() + max(0.0, args.submit_interval_seconds)
    print(
        f"SUBMITTED {submitted_count:03d}: {job.category_slug}/{job.filename} "
        f"(next submit >= {args.submit_interval_seconds:.1f}s)",
        flush=True,
    )
    return submitted_count, next_submit_at, True


def wait_timeout(
    futures: dict[Future[GeneratedImage], ImageJob],
    pending_jobs: list[ImageJob],
    args: argparse.Namespace,
    deadline_at: float | None,
    submitted_count: int,
    next_submit_at: float,
) -> float:
    timeout = 5.0
    now = time.monotonic()
    if pending_jobs and len(futures) < args.workers:
        if args.max_images is None or submitted_count < args.max_images:
            timeout = min(timeout, max(0.0, next_submit_at - now))
    if deadline_at is not None:
        if futures and now >= deadline_at:
            return timeout
        timeout = min(timeout, max(0.0, deadline_at - now))
    return timeout


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    if args.submit_interval_seconds < 0:
        raise SystemExit("--submit-interval-seconds must be non-negative")

    if args.refresh_prompts:
        refresh_prompt_file(args.prompts_url, args.prompts_path)

    prompt_data = load_prompt_data(args.prompts_path)
    selected_categories = set(args.categories) if args.categories else None
    unknown_categories = selected_categories - set(CATEGORY_ORDER) if selected_categories else set()
    if unknown_categories:
        raise SystemExit(f"Unknown categories: {', '.join(sorted(unknown_categories))}")

    jobs = build_jobs(prompt_data, selected_categories)
    successful_keys = load_successful_keys(args.manifest_path)
    bucket = ensure_env("R2_BUCKET")
    s3_client = build_s3_client()

    if args.dry_run:
        r2_existing_keys = set() if args.skip_existing_r2_check else list_existing_r2_keys(s3_client, bucket, selected_categories)
        missing_jobs = [job for job in jobs if job.r2_key not in successful_keys and job.r2_key not in r2_existing_keys]
        print(json.dumps({
            "planned_total": len(jobs),
            "manifest_uploaded": len(successful_keys),
            "r2_existing_matching_jobs": sum(1 for job in jobs if job.r2_key in r2_existing_keys),
            "missing_total": len(missing_jobs),
            "next_missing": missing_jobs[0].filename if missing_jobs else None,
            "uploaded_by_category": uploaded_counts(jobs, successful_keys | r2_existing_keys),
        }, indent=2))
        return 0

    r2_existing_keys = set() if args.skip_existing_r2_check else list_existing_r2_keys(s3_client, bucket, selected_categories)
    skipped_manifest = 0
    skipped_existing_r2 = 0
    for job in jobs:
        if job.r2_key in successful_keys:
            skipped_manifest += 1
            continue
        if job.r2_key in r2_existing_keys:
            record_r2_preexisting(job, args.manifest_path, args.model)
            successful_keys.add(job.r2_key)
            skipped_existing_r2 += 1
            print(f"SKIP existing on R2: {job.r2_key}", flush=True)

    pending_jobs = [job for job in jobs if job.r2_key not in successful_keys]
    write_summary(
        args.summary_path,
        failure_log_path=args.failure_log_path,
        jobs=jobs,
        successful_keys=successful_keys,
        model=args.model,
        size=args.size,
        quality=args.quality,
        submit_interval_seconds=args.submit_interval_seconds,
        attempted=0,
        uploaded=0,
        skipped_manifest=skipped_manifest,
        skipped_existing_r2=skipped_existing_r2,
    )

    deadline_at = time.monotonic() + args.max_runtime_seconds if args.max_runtime_seconds else None
    attempted = 0
    uploaded = 0
    submitted = 0
    terminal_message: str | None = None

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures: dict[Future[GeneratedImage], ImageJob] = {}
        next_submit_at = 0.0
        deadline_notice_printed = False
        while pending_jobs or futures:
            submitted, next_submit_at, did_submit = submit_one_if_ready(
                executor, futures, pending_jobs, args, deadline_at, submitted, next_submit_at
            )
            if did_submit:
                continue

            timeout = wait_timeout(futures, pending_jobs, args, deadline_at, submitted, next_submit_at)
            if futures:
                completed_futures, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
            else:
                if timeout > 0:
                    time.sleep(timeout)
                completed_futures = set()

            if not completed_futures:
                if deadline_at is not None and time.monotonic() >= deadline_at:
                    if futures:
                        if not deadline_notice_printed:
                            print("Runtime budget reached; waiting for in-flight generations to finish.", flush=True)
                            deadline_notice_printed = True
                        continue
                    break
                if args.max_images is not None and submitted >= args.max_images and not futures:
                    break
                if not pending_jobs and not futures:
                    break
                continue

            for future in completed_futures:
                job = futures.pop(future)
                attempted += 1
                try:
                    result = future.result()
                    local_path = args.cache_dir / job.category_slug / job.filename
                    save_as_jpeg(result.image_bytes, local_path)
                    upload_image(s3_client, bucket, local_path, job.r2_key)
                    record_upload(result, local_path, args.manifest_path, args.model, args.size, args.quality)
                    successful_keys.add(job.r2_key)
                    uploaded += 1
                    print(
                        f"UPLOADED {uploaded:03d} this run | {job.category_slug}/{job.filename} "
                        f"in {result.elapsed_seconds:.1f}s",
                        flush=True,
                    )
                except (RateLimitError, BadRequestError, APIStatusError, APIConnectionError, requests.RequestException, ClientError, OSError, RuntimeError) as exc:
                    record_failure(job, args.failure_log_path, args.model, args.size, args.quality, exc)
                    print(f"FAILED {job.category_slug}/{job.filename}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    if terminal_error(exc):
                        terminal_message = f"{type(exc).__name__}: {exc}"
                write_summary(
                    args.summary_path,
                    failure_log_path=args.failure_log_path,
                    jobs=jobs,
                    successful_keys=successful_keys,
                    model=args.model,
                    size=args.size,
                    quality=args.quality,
                    submit_interval_seconds=args.submit_interval_seconds,
                    attempted=attempted,
                    uploaded=uploaded,
                    skipped_manifest=skipped_manifest,
                    skipped_existing_r2=skipped_existing_r2,
                    terminal_error_message=terminal_message,
                )
                if terminal_message:
                    for pending_future in futures:
                        pending_future.cancel()
                    futures.clear()
                    pending_jobs.clear()
                    break
            if terminal_message:
                break

    write_summary(
        args.summary_path,
        failure_log_path=args.failure_log_path,
        jobs=jobs,
        successful_keys=successful_keys,
        model=args.model,
        size=args.size,
        quality=args.quality,
        submit_interval_seconds=args.submit_interval_seconds,
        attempted=attempted,
        uploaded=uploaded,
        skipped_manifest=skipped_manifest,
        skipped_existing_r2=skipped_existing_r2,
        terminal_error_message=terminal_message,
    )
    print(f"Run complete. Uploaded {uploaded} images this run. Attempted {attempted}.", flush=True)
    return 2 if terminal_message else 0


if __name__ == "__main__":
    raise SystemExit(main())
