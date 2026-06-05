import os
from datetime import datetime, timezone
from fastapi import APIRouter
from sqlalchemy import text
from src.api.database import SessionLocal
from src.api.usage import (
    BRAVE_MONTHLY_LIMIT, WEBSHARE_BYTES_PER_CALL,
    CLAUDE_HAIKU_INPUT_COST_PER_M, CLAUDE_HAIKU_OUTPUT_COST_PER_M,
    fetch_webshare_live, fetch_anthropic_live,
)

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("")
def get_usage():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month = today[:7]  # "2026-06"

    with SessionLocal() as db:
        # Daily breakdown last 30 days
        rows = db.execute(text("""
            SELECT date, service, calls, bytes_est, tokens_in, tokens_out
            FROM api_usage
            WHERE date >= date('now', '-30 days')
            ORDER BY date DESC, service
        """)).fetchall()

        # Monthly totals from local tracking
        monthly = db.execute(text("""
            SELECT service, SUM(calls), SUM(bytes_est), SUM(tokens_in), SUM(tokens_out)
            FROM api_usage
            WHERE date LIKE :month
            GROUP BY service
        """), {"month": f"{month}%"}).fetchall()

        # Cached quota data (from response headers / last live fetch)
        quota_rows = db.execute(text("""
            SELECT service, quota_used, quota_limit, quota_remaining,
                   bandwidth_bytes, tokens_in, tokens_out, cost_usd, updated_at
            FROM api_quota
        """)).fetchall()

    # Build daily rows keyed by date
    by_date = {}
    for r in rows:
        d = r[0]
        if d not in by_date:
            by_date[d] = {
                "date": d,
                "brave": 0, "webshare": 0, "webshare_mb": 0,
                "claude_calls": 0, "claude_tokens_in": 0,
                "claude_tokens_out": 0, "claude_cost_usd": 0.0,
            }
        svc = r[1]
        if svc == "brave":
            by_date[d]["brave"] = r[2]
        elif svc == "webshare":
            by_date[d]["webshare"] = r[2]
            by_date[d]["webshare_mb"] = round(r[3] / 1_048_576, 2)
        elif svc == "claude":
            tin, tout = r[4], r[5]
            by_date[d]["claude_calls"] = r[2]
            by_date[d]["claude_tokens_in"] = tin
            by_date[d]["claude_tokens_out"] = tout
            by_date[d]["claude_cost_usd"] = round(
                tin / 1_000_000 * CLAUDE_HAIKU_INPUT_COST_PER_M +
                tout / 1_000_000 * CLAUDE_HAIKU_OUTPUT_COST_PER_M, 4
            )

    monthly_totals = {
        "brave": 0, "webshare": 0, "webshare_mb": 0,
        "claude_calls": 0, "claude_tokens_in": 0,
        "claude_tokens_out": 0, "claude_cost_usd": 0.0,
    }
    for r in monthly:
        svc = r[0]
        if svc == "brave":
            monthly_totals["brave"] = r[1]
        elif svc == "webshare":
            monthly_totals["webshare"] = r[1]
            monthly_totals["webshare_mb"] = round(r[2] / 1_048_576, 2)
        elif svc == "claude":
            tin, tout = r[3], r[4]
            monthly_totals["claude_calls"] = r[1]
            monthly_totals["claude_tokens_in"] = tin
            monthly_totals["claude_tokens_out"] = tout
            monthly_totals["claude_cost_usd"] = round(
                tin / 1_000_000 * CLAUDE_HAIKU_INPUT_COST_PER_M +
                tout / 1_000_000 * CLAUDE_HAIKU_OUTPUT_COST_PER_M, 4
            )

    # Cached quota from api_quota (Brave from response headers, others from last live fetch)
    quota = {r[0]: {
        "quota_used": r[1], "quota_limit": r[2], "quota_remaining": r[3],
        "bandwidth_bytes": r[4], "tokens_in": r[5], "tokens_out": r[6],
        "cost_usd": r[7], "updated_at": r[8],
    } for r in quota_rows}

    brave_quota = quota.get("brave", {})
    brave_limit = brave_quota.get("quota_limit") or BRAVE_MONTHLY_LIMIT
    brave_remaining = brave_quota.get("quota_remaining", max(0, brave_limit - monthly_totals["brave"]))
    brave_used_live = brave_quota.get("quota_used", monthly_totals["brave"])
    brave_pct = round(brave_used_live / brave_limit * 100, 1) if brave_limit else 0

    # Determine which keys are configured (so dashboard can show correct badges)
    has_webshare_key = bool(os.getenv("WEBSHARE_API_KEY", ""))
    has_anthropic_admin_key = bool(os.getenv("ANTHROPIC_ADMIN_KEY", ""))

    return {
        "month": month,
        "monthly": monthly_totals,
        "brave_limit": brave_limit,
        "brave_remaining": brave_remaining,
        "brave_used_live": brave_used_live,
        "brave_pct": brave_pct,
        "brave_quota_updated_at": brave_quota.get("updated_at"),
        "webshare_live": quota.get("webshare"),      # None if not yet fetched
        "claude_live": quota.get("claude"),           # None if not yet fetched
        "has_webshare_key": has_webshare_key,
        "has_anthropic_admin_key": has_anthropic_admin_key,
        "daily": list(by_date.values()),
    }


@router.post("/refresh")
def refresh_live_usage():
    """
    Trigger a live fetch from Webshare and Anthropic billing APIs.
    Returns the fetched data (or null per service if key not configured).
    """
    webshare = fetch_webshare_live()
    anthropic = fetch_anthropic_live()
    return {
        "webshare": webshare,
        "anthropic": anthropic,
        "webshare_key_set": bool(os.getenv("WEBSHARE_API_KEY", "")),
        "anthropic_admin_key_set": bool(os.getenv("ANTHROPIC_ADMIN_KEY", "")),
    }
