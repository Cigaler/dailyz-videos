#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import textwrap
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

import boto3
from botocore.exceptions import ClientError
from openai import OpenAI

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MEMORY_KEY = "3 - Production/creative_agent/memory.json"
ARCHIVE_PREFIX = "3 - Production/creative_agent/memory_archive"
ROLLING_CONTEXT_SESSIONS = 5
ACTIVE_SESSION_LIMIT = 20

AGENT_PROFILE = """You are ZEUS — Creative Director & Strategic Advisor for DailyZ, a zero-budget TikTok/YouTube Shorts studio.

YOUR ROLE:
- Creative Director: Generate viral video concepts, scripts, loop specs, image prompts, thumbnail ideas
- Strategic Advisor: Analyze situations, challenge assumptions, give blunt actionable recommendations
- Performance Coach: Track what works and what doesn't, improve your own advice over time

YOUR PERSONALITY:
- You are NOT a yes-man. You push back when you see a bad idea.
- You are direct, specific, and technical. No fluff, no hedging.
- You think like a data-driven creator who has studied thousands of viral videos.
- You balance creativity with efficiency — this is a LEAN studio, not a Hollywood production.
- You remember every conversation and every piece of advice you've given.
- You measure your own performance and get better every cycle.

YOUR EXPERTISE:
- Short-form video (TikTok, YouTube Shorts) viral mechanics
- FFmpeg/Python video production pipelines
- DALL-E prompt engineering for background images
- ElevenLabs voice optimization
- Hook writing, retention curves, CTA optimization
- Content calendars and topic clustering for algorithmic growth
- Thumbnail psychology and click-through optimization

CORE CONSTRAINT:
- Budget: ~$30/month total infrastructure
- Tools: OpenAI API, ElevenLabs, FFmpeg, Python, Cloudflare R2
- No manual labor except: owner uploads videos to TikTok/YouTube

YOUR SELF-IMPROVEMENT PROTOCOL:
- Every piece of advice you give is logged with an advice_id
- You will be asked to report on outcomes (what worked, what didn't, score 1-10)
- You use this feedback to refine your future recommendations
- You track your own hit rate and flag when patterns emerge

MEMORY RULE:
- Always reference previous conversations when relevant
- Explicitly say "based on what worked in [previous context]..." or "last time X failed because..."
- Never repeat a bad recommendation twice
"""

BUSINESS_CONTEXT = """BUSINESS CONTEXT (always active):

COMPANY: DailyZ — TikTok/YouTube Shorts studio, zero budget, building from scratch
CHANNELS: @Cigalerr (YouTube) | @cigaler (TikTok)
PRIMARY GOAL: Monetization — YouTube 500 subs + 3M Shorts views OR 3K watch hours | TikTok 10K followers + 100K views/30d

CONTENT:
- 15 categories: tech_ai, finance, motivation, history, science, space, nature, psychology, mysteries, geography, luxury, futurism, health, productivity, abstract
- Format: 45-90 second narrated shorts with animated loop backgrounds + still images
- Voice: Chris (ElevenLabs) — calm, authoritative
- Style: Dark background (#0A0A0F), white text, cyan keywords (#00E5FF)

PRODUCTION PIPELINE:
1. GPT-4o generates scripts → ElevenLabs TTS → FFmpeg renders video → DALL-E 3 thumbnail → R2 → owner uploads manually

ASSET LIBRARY (Project Boost):
- 50 animated loops (10 styles × 5 variations): bokeh, light, nebulae, smoke, particles, gradients, lines, futuristic, geometric, neural
- 300 still images (20 per category)
- Status: ~39/300 images done, loops in progress

CURRENT PRIORITY: Complete Project Boost asset library (loops + images) THEN resume video production at 3+ videos/day

WHAT "GOOD" LOOKS LIKE:
- Loops: minimalist, not cheap — professional motion graphics quality
- Images: 20 genuinely different subjects per category (NOT 20 variations of the same thing)
- Scripts: hook in first 3 seconds, evergreen topics, clear CTA
- Thumbnails: bold, high contrast, curiosity gap

WHAT HAS FAILED:
- Lines, futuristic, geometric, neural loops were generated at low quality — needed complete redo
- First image batch lacked diversity (too many similar compositions per category)

YOUR JOB RIGHT NOW: Be the creative brain and strategic compass for every decision in this studio.
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable: {name}")


def summarize_text(value: str, limit: int = 160) -> str:
    compact = " ".join(value.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


class CreativeAdvisorAgent:
    def __init__(self) -> None:
        self.model = OPENAI_MODEL
        self.client = OpenAI(api_key=ensure_env("OPENAI_API_KEY"))
        self.bucket = ensure_env("R2_BUCKET")
        endpoint = os.getenv("R2_ENDPOINT_URL") or os.getenv("R2_ENDPOINT")
        access_key = os.getenv("R2_ACCESS_KEY_ID")
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        missing = [
            name
            for name, value in (
                ("R2_ENDPOINT_URL or R2_ENDPOINT", endpoint),
                ("R2_ACCESS_KEY_ID", access_key),
                ("R2_SECRET_ACCESS_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required R2 configuration: {', '.join(missing)}")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        self.profile = AGENT_PROFILE
        self.context = BUSINESS_CONTEXT
        self.memory = self._load_memory()
        self._recalculate_performance_stats()

    def ask(self, question: str) -> str:
        advice_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        response_text = self._run_model(
            prompt=question,
            extra_messages=[
                {
                    "role": "user",
                    "content": self._build_memory_context(),
                }
            ],
        )

        session = {
            "session_id": session_id,
            "timestamp": utc_now_iso(),
            "topic": summarize_text(question, limit=96),
            "messages": [
                {"role": "user", "content": question},
                {"role": "assistant", "content": response_text},
            ],
            "advice_given": [
                {
                    "advice_id": advice_id,
                    "summary": summarize_text(response_text, limit=140),
                    "category": self._classify_category(question, response_text),
                    "status": "pending",
                    "outcome_score": None,
                    "outcome_notes": None,
                    "measured_at": None,
                    "reflection": None,
                }
            ],
        }
        self.memory["sessions"].append(session)
        self._save_memory()
        return f"Advice ID: {advice_id}\n\n{response_text.strip()}"

    def report_result(self, advice_id: str, outcome: dict[str, Any]) -> None:
        advice_entry = self._find_advice(advice_id)
        advice_entry["status"] = "measured"
        advice_entry["outcome_score"] = int(outcome["score"])
        advice_entry["outcome_notes"] = outcome.get("notes")
        advice_entry["measured_at"] = utc_now_iso()
        if outcome.get("what_worked") is not None:
            advice_entry["what_worked"] = outcome.get("what_worked")
        if outcome.get("what_failed") is not None:
            advice_entry["what_failed"] = outcome.get("what_failed")

        reflection_prompt = textwrap.dedent(
            f"""
            Here is feedback on advice #{advice_id}: {json.dumps(outcome, ensure_ascii=True)}.
            What does this tell you about your approach? Update your internal strategy for future recommendations.
            Keep it concise, specific, and focused on future recommendation quality.
            """
        ).strip()
        advice_entry["reflection"] = self._run_model(
            prompt=reflection_prompt,
            extra_messages=[
                {
                    "role": "user",
                    "content": self._build_memory_context(),
                }
            ],
        )
        self._save_memory()

    def get_performance_summary(self) -> dict[str, Any]:
        stats = self.memory["performance_stats"]
        hit_rate = stats["total_measured"] / stats["total_advice_given"] * 100 if stats["total_advice_given"] else 0.0
        category_scores = {
            category: details["avg_score"]
            for category, details in stats["hit_rate_by_category"].items()
            if details["avg_score"] is not None
        }
        best_categories = sorted(category_scores.items(), key=lambda item: item[1], reverse=True)[:3]
        worst_categories = sorted(category_scores.items(), key=lambda item: item[1])[:3]
        reflections = self._recent_reflections(limit=3)

        report = "\n".join(
            [
                "ZEUS Performance Report",
                "======================",
                f"Total advice given: {stats['total_advice_given']}",
                f"Measured: {stats['total_measured']} ({hit_rate:.1f}%)",
                f"Average score: {stats['avg_score']:.2f}/10" if stats["avg_score"] is not None else "Average score: N/A",
                "Best categories: "
                + (", ".join(f"{name} ({score:.2f})" for name, score in best_categories) if best_categories else "N/A"),
                "Worst categories: "
                + (", ".join(f"{name} ({score:.2f})" for name, score in worst_categories) if worst_categories else "N/A"),
                "Key patterns: "
                + (" | ".join(stats["patterns_identified"]) if stats["patterns_identified"] else "N/A"),
                "Recent reflections: "
                + (" | ".join(reflections) if reflections else "N/A"),
            ]
        )
        return {
            "report": report,
            "total_advice_given": stats["total_advice_given"],
            "total_measured": stats["total_measured"],
            "avg_score": stats["avg_score"],
            "best_categories": best_categories,
            "worst_categories": worst_categories,
            "patterns_identified": stats["patterns_identified"],
            "recent_reflections": reflections,
        }

    def upload_json(self, key: str, payload: dict[str, Any]) -> None:
        self._put_json(key, payload)

    def _load_memory(self) -> dict[str, Any]:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=MEMORY_KEY)["Body"].read()
            loaded = json.loads(body.decode("utf-8"))
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code not in {"NoSuchKey", "404", "NotFound"}:
                raise
            loaded = self._empty_memory()
        return self._normalize_memory(loaded)

    def _save_memory(self) -> None:
        self._archive_old_sessions()
        self._recalculate_performance_stats()
        self._put_json(MEMORY_KEY, self.memory)

    def _archive_old_sessions(self) -> None:
        sessions = self.memory["sessions"]
        if len(sessions) <= ACTIVE_SESSION_LIMIT:
            return

        to_archive = sessions[:-ACTIVE_SESSION_LIMIT]
        self.memory["sessions"] = sessions[-ACTIVE_SESSION_LIMIT:]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in to_archive:
            month = session["timestamp"][:7]
            grouped[month].append(session)

        for month, month_sessions in grouped.items():
            archive_key = f"{ARCHIVE_PREFIX}/{month}.json"
            archive_payload = self._load_archive(archive_key)
            existing_ids = {session["session_id"] for session in archive_payload["sessions"]}
            archive_payload["sessions"].extend(
                session for session in month_sessions if session["session_id"] not in existing_ids
            )
            self._put_json(archive_key, archive_payload)

    def _load_archive(self, key: str) -> dict[str, Any]:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            loaded = json.loads(body.decode("utf-8"))
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code not in {"NoSuchKey", "404", "NotFound"}:
                raise
            loaded = {"sessions": []}
        if not isinstance(loaded.get("sessions"), list):
            loaded["sessions"] = []
        return loaded

    def _put_json(self, key: str, payload: dict[str, Any]) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, indent=2, ensure_ascii=True).encode("utf-8"),
            ContentType="application/json",
        )

    def _run_model(self, prompt: str, extra_messages: list[dict[str, str]] | None = None) -> str:
        messages = [
            self._message("system", self.profile),
            self._message("user", self.context),
        ]
        for message in extra_messages or []:
            messages.append(self._message(message["role"], message["content"]))
        messages.append(self._message("user", prompt))
        response = self.client.responses.create(
            model=self.model,
            input=messages,
        )
        return response.output_text.strip()

    def _message(self, role: str, content: str) -> dict[str, Any]:
        return {
            "role": role,
            "content": [
                {
                    "type": "input_text",
                    "text": content,
                }
            ],
        }

    def _build_memory_context(self) -> str:
        recent_sessions = self.memory["sessions"][-ROLLING_CONTEXT_SESSIONS:]
        if not recent_sessions:
            return "MEMORY CONTEXT:\nNo previous sessions yet."

        lines = ["MEMORY CONTEXT:"]
        for session in recent_sessions:
            lines.append(f"- Session {session['session_id']} at {session['timestamp']}: {session['topic']}")
            for advice in session.get("advice_given", []):
                status_bits = [advice["status"]]
                if advice.get("outcome_score") is not None:
                    status_bits.append(f"score {advice['outcome_score']}/10")
                if advice.get("outcome_notes"):
                    status_bits.append(f"notes: {summarize_text(advice['outcome_notes'], 100)}")
                if advice.get("reflection"):
                    status_bits.append(f"reflection: {summarize_text(advice['reflection'], 100)}")
                lines.append(
                    f"  advice {advice['advice_id']} [{advice['category']}]: {advice['summary']} ({'; '.join(status_bits)})"
                )
            for message in session.get("messages", [])[-2:]:
                lines.append(f"  {message['role']}: {summarize_text(message['content'], 120)}")
        return "\n".join(lines)

    def _find_advice(self, advice_id: str) -> dict[str, Any]:
        for session in self.memory["sessions"]:
            for advice in session.get("advice_given", []):
                if advice.get("advice_id") == advice_id:
                    return advice
        raise ValueError(f"Advice ID not found: {advice_id}")

    def _recalculate_performance_stats(self) -> None:
        advice_items = [advice for session in self.memory["sessions"] for advice in session.get("advice_given", [])]
        measured = [advice for advice in advice_items if advice.get("status") == "measured" and advice.get("outcome_score") is not None]

        category_scores: dict[str, list[int]] = defaultdict(list)
        for advice in measured:
            category_scores[advice["category"]].append(int(advice["outcome_score"]))

        hit_rate_by_category: dict[str, dict[str, Any]] = {}
        for category, scores in sorted(category_scores.items()):
            avg_score = mean(scores)
            hit_rate_by_category[category] = {
                "measured": len(scores),
                "avg_score": round(avg_score, 2),
                "hit_rate": round(sum(1 for score in scores if score >= 7) / len(scores) * 100, 1),
            }

        avg_score = round(mean(int(advice["outcome_score"]) for advice in measured), 2) if measured else None
        self.memory["performance_stats"] = {
            "total_advice_given": len(advice_items),
            "total_measured": len(measured),
            "avg_score": avg_score,
            "hit_rate_by_category": hit_rate_by_category,
            "patterns_identified": self._identify_patterns(hit_rate_by_category, measured),
        }

    def _identify_patterns(
        self, hit_rate_by_category: dict[str, dict[str, Any]], measured: list[dict[str, Any]]
    ) -> list[str]:
        patterns: list[str] = []
        if hit_rate_by_category:
            ranked = sorted(
                hit_rate_by_category.items(),
                key=lambda item: item[1]["avg_score"],
                reverse=True,
            )
            best_category, best_stats = ranked[0]
            patterns.append(
                f"Best measured category is {best_category} with average score {best_stats['avg_score']}/10."
            )
            if len(ranked) > 1:
                worst_category, worst_stats = ranked[-1]
                patterns.append(
                    f"Weakest measured category is {worst_category} with average score {worst_stats['avg_score']}/10."
                )

        reflections = [summarize_text(advice["reflection"], 120) for advice in measured if advice.get("reflection")]
        patterns.extend(reflections[-3:])
        return patterns[:5]

    def _recent_reflections(self, limit: int) -> list[str]:
        reflections = []
        for session in reversed(self.memory["sessions"]):
            for advice in reversed(session.get("advice_given", [])):
                if advice.get("reflection"):
                    reflections.append(summarize_text(advice["reflection"], 120))
                if len(reflections) >= limit:
                    return reflections
        return reflections

    def _classify_category(self, question: str, response_text: str) -> str:
        haystack = f"{question} {response_text}".lower()
        keyword_map = {
            "loops": ("loop", "motion graphics", "animation", "animated"),
            "images": ("image", "dall-e", "background", "still", "visual", "prompt"),
            "scripts": ("script", "hook", "cta", "retention"),
            "strategy": ("strategy", "calendar", "growth", "monetization", "channel"),
            "thumbnails": ("thumbnail", "ctr", "click-through"),
        }
        for category, keywords in keyword_map.items():
            if any(keyword in haystack for keyword in keywords):
                return category
        return "other"

    def _empty_memory(self) -> dict[str, Any]:
        return {
            "sessions": [],
            "performance_stats": {
                "total_advice_given": 0,
                "total_measured": 0,
                "avg_score": None,
                "hit_rate_by_category": {},
                "patterns_identified": [],
            },
        }

    def _normalize_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._empty_memory()
        if isinstance(payload.get("sessions"), list):
            normalized["sessions"] = payload["sessions"]
        if isinstance(payload.get("performance_stats"), dict):
            normalized["performance_stats"].update(payload["performance_stats"])
        return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent GPT-4o creative/advisor agent for DailyZ.")
    parser.add_argument("--ask", help="Ask ZEUS a question and save the response to R2-backed memory.")
    parser.add_argument("--report", action="store_true", help="Report a measured outcome for a prior advice_id.")
    parser.add_argument("--advice-id", help="Advice ID to update when using --report.")
    parser.add_argument("--score", type=int, help="Outcome score from 1-10 when using --report.")
    parser.add_argument("--notes", help="Outcome notes when using --report.")
    parser.add_argument("--what-worked", help="What worked for this advice outcome.")
    parser.add_argument("--what-failed", help="What failed for this advice outcome.")
    parser.add_argument("--performance", action="store_true", help="Print the current ZEUS performance summary.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    agent = CreativeAdvisorAgent()

    if args.ask:
        print(agent.ask(args.ask))
        return 0

    if args.report:
        if not args.advice_id or args.score is None:
            parser.error("--report requires --advice-id and --score")
        outcome = {
            "score": args.score,
            "notes": args.notes or "",
            "what_worked": args.what_worked or "",
            "what_failed": args.what_failed or "",
        }
        agent.report_result(args.advice_id, outcome)
        print(f"Reported outcome for advice {args.advice_id}.")
        return 0

    if args.performance:
        print(agent.get_performance_summary()["report"])
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
