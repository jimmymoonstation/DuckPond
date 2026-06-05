"""Lightweight API usage tracker — records daily call counts for Brave, Webshare, and Claude."""

from datetime import datetime, timezone

from sqlalchemy import text

from src.api.database import SessionLocal

# Brave Search API: free tier is 2,000 queries/month
BRAVE_MONTHLY_LIMIT = 2000

# Webshare residential proxy — estimated bytes per DDG call (~10KB avg response)
WEBSHARE_BYTES_PER_CALL = 10_240

# Claude Haiku pricing (per million tokens, as of 2025)
CLAUDE_HAIKU_INPUT_COST_PER_M  = 0.80   # $0.80 / 1M input tokens
CLAUDE_HAIKU_OUTPUT_COST_PER_M = 4.00   # $4.00 / 1M output tokens


def record(service: str, calls: int = 1, bytes_est: int = 0,
           tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Increment the daily counter for a service. Fire-and-forget — never raises."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO api_usage (date, service, calls, bytes_est, tokens_in, tokens_out)
                VALUES (:date, :service, :calls, :bytes, :tokens_in, :tokens_out)
                ON CONFLICT(date, service) DO UPDATE SET
                    calls      = calls      + excluded.calls,
                    bytes_est  = bytes_est  + excluded.bytes_est,
                    tokens_in  = tokens_in  + excluded.tokens_in,
                    tokens_out = tokens_out + excluded.tokens_out
            """), {
                "date": today, "service": service, "calls": calls,
                "bytes": bytes_est, "tokens_in": tokens_in, "tokens_out": tokens_out,
            })
            db.commit()
    except Exception:
        pass  # never let tracking break the caller


def record_brave(calls: int = 1) -> None:
    record("brave", calls=calls, bytes_est=calls * 2_048)  # ~2KB per Brave result set


def record_webshare(calls: int = 1) -> None:
    record("webshare", calls=calls, bytes_est=calls * WEBSHARE_BYTES_PER_CALL)


def record_claude(input_tokens: int, output_tokens: int) -> None:
    """Record a Claude API call with exact token counts."""
    record("claude", calls=1, tokens_in=input_tokens, tokens_out=output_tokens)


# ── Live billing API fetchers ────────────────────────────────────────────────

def fetch_webshare_live() -> dict | None:
    """
    Fetch real bandwidth and request stats from the Webshare aggregate API.
    Requires WEBSHARE_API_KEY env var. Returns None on failure or missing key.
    """
    import os
    import httpx
    from datetime import datetime, timezone, timedelta

    key = os.getenv("WEBSHARE_API_KEY", "")
    if not key:
        return None
    try:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        resp = httpx.get(
            "https://proxy.webshare.io/api/v2/stats/aggregate/",
            headers={"Authorization": f"Token {key}"},
            params={
                "timestamp__gte": month_start.isoformat(),
                "timestamp__lte": now.isoformat(),
            },
            timeout=10,
        )
        if not resp.is_success:
            return None
        data = resp.json()
        bandwidth_bytes = data.get("bandwidth_total", 0) or 0
        requests_total = data.get("requests_total", 0) or 0

        # Cache in api_quota table
        from src.api.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO api_quota (service, quota_used, bandwidth_bytes, updated_at)
                VALUES ('webshare', :calls, :bytes, :ts)
                ON CONFLICT(service) DO UPDATE SET
                    quota_used     = excluded.quota_used,
                    bandwidth_bytes = excluded.bandwidth_bytes,
                    updated_at     = excluded.updated_at
            """), {
                "calls": requests_total,
                "bytes": bandwidth_bytes,
                "ts": now.isoformat(),
            })
            db.commit()
        return {"requests_total": requests_total, "bandwidth_bytes": bandwidth_bytes}
    except Exception:
        return None


def fetch_anthropic_live() -> dict | None:
    """
    Fetch real token usage from the Anthropic Admin API.
    Requires ANTHROPIC_ADMIN_KEY env var (sk-ant-admin...).
    Only available for organization accounts. Returns None on failure or missing key.
    """
    import os
    import httpx
    from datetime import datetime, timezone

    key = os.getenv("ANTHROPIC_ADMIN_KEY", "")
    if not key:
        return None
    try:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        resp = httpx.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
            params={
                "starting_at": month_start.strftime("%Y-%m-%dT00:00:00Z"),
                "ending_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bucket_width": "1d",
            },
            timeout=15,
        )
        if not resp.is_success:
            return None
        buckets = resp.json().get("data", [])
        total_in = sum(b.get("input_tokens", 0) + b.get("cache_read_input_tokens", 0) for b in buckets)
        total_out = sum(b.get("output_tokens", 0) for b in buckets)
        total_calls = sum(b.get("request_count", 0) for b in buckets)

        cost = round(
            total_in / 1_000_000 * CLAUDE_HAIKU_INPUT_COST_PER_M +
            total_out / 1_000_000 * CLAUDE_HAIKU_OUTPUT_COST_PER_M, 4
        )

        # Cache in api_quota table
        from src.api.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO api_quota (service, quota_used, tokens_in, tokens_out, cost_usd, updated_at)
                VALUES ('claude', :calls, :tin, :tout, :cost, :ts)
                ON CONFLICT(service) DO UPDATE SET
                    quota_used = excluded.quota_used,
                    tokens_in  = excluded.tokens_in,
                    tokens_out = excluded.tokens_out,
                    cost_usd   = excluded.cost_usd,
                    updated_at = excluded.updated_at
            """), {
                "calls": total_calls, "tin": total_in, "tout": total_out,
                "cost": cost, "ts": now.isoformat(),
            })
            db.commit()
        return {"calls": total_calls, "tokens_in": total_in, "tokens_out": total_out, "cost_usd": cost}
    except Exception:
        return None
