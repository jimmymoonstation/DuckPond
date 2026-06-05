"""
Job Hunt Module for the Discord bot.
Handles all messages in #job-hunt channel with conversational coaching via Claude.
"""
import json
import logging
import os
from datetime import datetime

import anthropic
import httpx

logger = logging.getLogger(__name__)

API_BASE = "http://127.0.0.1:5057/api"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAX_HISTORY = 20  # messages to retain per channel


def _make_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


async def handle_message(channel_id: str, user_content: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "Claude API key not configured. Add ANTHROPIC_API_KEY to .env"

    stats = await _get_stats()
    new_jobs = await _get_new_jobs()
    next_interview = await _get_next_interview()

    system = _build_system_prompt(stats, new_jobs, next_interview)
    history = await _load_history(channel_id)

    messages = history + [{"role": "user", "content": user_content}]

    try:
        client = _make_client()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=system,
            messages=messages,
        )
        assistant_text = resp.content[0].text
        try:
            from src.api.usage import record_claude
            record_claude(resp.usage.input_tokens, resp.usage.output_tokens)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return "Sorry, I hit an error reaching Claude. Try again in a moment."

    await _save_history(channel_id, user_content, assistant_text)
    return assistant_text


def _build_system_prompt(stats: dict, new_jobs: list, next_interview: dict | None) -> str:
    days_remaining = stats.get("days_remaining", 60)
    days_elapsed = stats.get("days_since_start", 1)
    apps_total = stats.get("applications_total", 0)
    apps_week = stats.get("applications_total", 0)  # this is already week-scoped when called with period=week
    interviews = stats.get("interviews_scheduled", 0)
    daily_avg = stats.get("daily_average_applications", 0)

    interview_line = ""
    if next_interview:
        interview_line = f"- Next interview: {next_interview.get('round')} @ {next_interview.get('company')} on {next_interview.get('scheduled_at')}\n"

    new_jobs_line = f"- New job openings since last check: {len(new_jobs)}\n" if new_jobs else ""

    return f"""You are a direct, supportive job hunting partner. The user is on a visa deadline.

Current status (today is day {days_elapsed} of their search):
- Days remaining to land a job: {days_remaining}
- Total applications submitted: {apps_total}
- Interviews scheduled (all time): {interviews}
- Daily average applications: {daily_avg}
{interview_line}{new_jobs_line}
Target: 3+ applications per day to hit the 2-month goal.

Your role:
- Be honest and direct. If they're behind, say so clearly but constructively.
- If they have an interview coming up, prioritize prep help.
- Answer questions about job searching, interview prep, resume, cover letters.
- If they mention applying somewhere, encourage them to log it in the dashboard.
- Keep responses under 200 words unless they ask for detail (like mock interview questions).
- No generic motivational fluff. Be specific and actionable.
- Today's date: {datetime.utcnow().strftime('%Y-%m-%d')}"""


async def _get_stats() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{API_BASE}/stats?period=week")
            return r.json()
    except Exception:
        return {}


async def _get_new_jobs() -> list:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{API_BASE}/jobs?status=new&limit=5")
            return r.json().get("jobs", [])
    except Exception:
        return []


async def _get_next_interview() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{API_BASE}/applications?status=interview&limit=10")
            apps = r.json().get("applications", [])
            for app in apps:
                for interview in app.get("interviews", []):
                    if interview.get("scheduled_at") and interview.get("outcome") is None:
                        return {
                            "round": interview["round"],
                            "company": app["job"]["company_name"],
                            "scheduled_at": interview["scheduled_at"],
                        }
    except Exception:
        pass
    return None


async def _load_history(channel_id: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{API_BASE}/../discord/history/{channel_id}")
            if r.status_code == 200:
                return r.json().get("history", [])
    except Exception:
        pass
    return []


async def _save_history(channel_id: str, user_msg: str, assistant_msg: str):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{API_BASE}/../discord/history/{channel_id}", json={
                "user": user_msg,
                "assistant": assistant_msg,
            })
    except Exception:
        pass
