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
