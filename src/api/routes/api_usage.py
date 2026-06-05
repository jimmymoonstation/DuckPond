from datetime import datetime, timezone
from fastapi import APIRouter
from sqlalchemy import text
from src.api.database import SessionLocal
from src.api.usage import (
    BRAVE_MONTHLY_LIMIT, WEBSHARE_BYTES_PER_CALL,
    CLAUDE_HAIKU_INPUT_COST_PER_M, CLAUDE_HAIKU_OUTPUT_COST_PER_M,
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

        # Monthly totals
        monthly = db.execute(text("""
            SELECT service, SUM(calls), SUM(bytes_est), SUM(tokens_in), SUM(tokens_out)
            FROM api_usage
            WHERE date LIKE :month
            GROUP BY service
        """), {"month": f"{month}%"}).fetchall()

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

    brave_remaining = max(0, BRAVE_MONTHLY_LIMIT - monthly_totals["brave"])
    brave_pct = round(monthly_totals["brave"] / BRAVE_MONTHLY_LIMIT * 100, 1)

    return {
        "month": month,
        "monthly": monthly_totals,
        "brave_limit": BRAVE_MONTHLY_LIMIT,
        "brave_remaining": brave_remaining,
        "brave_pct": brave_pct,
        "daily": list(by_date.values()),
    }
