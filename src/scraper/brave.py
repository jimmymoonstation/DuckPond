import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "X-Subscription-Token": BRAVE_API_KEY,
}


_brave_failures = 0
_BRAVE_FAILURE_LIMIT = 2


def reset_brave_circuit():
    global _brave_failures
    _brave_failures = 0


async def search_jobs(titles: list[str], locations: list[str], keywords: list[str] = None) -> list[dict]:
    """Run Brave Search queries and return raw job dicts."""
    global _brave_failures

    if not BRAVE_API_KEY:
        logger.warning("BRAVE_API_KEY not set — skipping Brave search")
        return []

    if _brave_failures >= _BRAVE_FAILURE_LIMIT:
        logger.info("Brave circuit breaker open — skipping Brave search this run")
        return []

    results = []
    queries = _build_queries(titles, locations, keywords or [])

    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for query in queries[:5]:
            try:
                resp = await client.get(
                    BRAVE_SEARCH_URL,
                    headers=HEADERS,
                    params={"q": query, "count": 10, "freshness": "pd"},
                )
                if resp.status_code in (301, 302, 303, 307, 308):
                    # Redirected away from API — key is invalid or expired
                    logger.warning(f"Brave API redirected ({resp.status_code}) — API key may be expired")
                    _brave_failures = _BRAVE_FAILURE_LIMIT  # trip immediately
                    break
                if resp.status_code == 429:
                    logger.warning("Brave API rate limited")
                    _brave_failures += 1
                    break
                if not resp.is_success:
                    _brave_failures += 1
                    break
                data = resp.json()
                _brave_failures = 0  # success — reset
                from src.api.usage import record_brave
                record_brave(calls=1)
                _store_brave_quota(resp.headers)
                urls = [r["url"] for r in data.get("web", {}).get("results", [])]
                for url in urls:
                    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as fetch_client:
                        job = await _fetch_and_parse(fetch_client, url)
                    if job:
                        results.append(job)
            except Exception as e:
                _brave_failures += 1
                logger.error(f"Brave search error for '{query}': {e}")
                if _brave_failures >= _BRAVE_FAILURE_LIMIT:
                    logger.warning("Brave circuit breaker tripped — skipping remaining queries")
                    break

    return results


def _store_brave_quota(headers) -> None:
    """Parse X-RateLimit-Remaining header and persist monthly quota to api_quota table."""
    try:
        # Header format: "X-RateLimit-Remaining: <per_second>, <monthly>"
        remaining_raw = headers.get("X-RateLimit-Remaining", "")
        limit_raw = headers.get("X-RateLimit-Limit", "")
        if not remaining_raw:
            return
        parts_remaining = [p.strip() for p in remaining_raw.split(",")]
        parts_limit = [p.strip() for p in limit_raw.split(",")]
        # Second value is the monthly window
        monthly_remaining = int(parts_remaining[-1]) if parts_remaining else 0
        monthly_limit = int(parts_limit[-1]) if parts_limit else BRAVE_MONTHLY_LIMIT
        monthly_used = max(0, monthly_limit - monthly_remaining)

        from src.api.database import SessionLocal
        from sqlalchemy import text
        from datetime import datetime, timezone
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO api_quota (service, quota_used, quota_limit, quota_remaining, updated_at)
                VALUES ('brave', :used, :limit, :remaining, :ts)
                ON CONFLICT(service) DO UPDATE SET
                    quota_used      = excluded.quota_used,
                    quota_limit     = excluded.quota_limit,
                    quota_remaining = excluded.quota_remaining,
                    updated_at      = excluded.updated_at
            """), {
                "used": monthly_used, "limit": monthly_limit,
                "remaining": monthly_remaining,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            db.commit()
    except Exception:
        pass


BRAVE_MONTHLY_LIMIT = 2000  # free tier default; overridden by live header data


def _build_queries(titles: list[str], locations: list[str], keywords: list[str]) -> list[str]:
    queries = []
    loc_str = " OR ".join(f'"{loc}"' for loc in locations) if locations else ""
    kw_str = " ".join(keywords[:3]) if keywords else ""

    for title in titles[:3]:
        q = f'"{title}" job opening'
        if loc_str:
            q += f" ({loc_str})"
        if kw_str:
            q += f" {kw_str}"
        q += " -site:linkedin.com -site:indeed.com"
        queries.append(q)

    return queries


async def _fetch_and_parse(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        resp = await client.get(url, follow_redirects=True, timeout=10)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")

        # Try JSON-LD structured data first (most reliable)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        return _parse_jsonld(item, url)
            except (json.JSONDecodeError, AttributeError):
                continue

        return None  # skip if no structured data
    except Exception as e:
        logger.debug(f"Failed to parse {url}: {e}")
        return None


def _parse_jsonld(data: dict, url: str) -> dict:
    org = data.get("hiringOrganization", {})
    location = data.get("jobLocation", {})
    if isinstance(location, list):
        location = location[0] if location else {}
    addr = location.get("address", {})

    loc_str = addr.get("addressLocality", "")
    if addr.get("addressRegion"):
        loc_str += f", {addr['addressRegion']}"
    if addr.get("addressCountry") and not loc_str:
        loc_str = addr["addressCountry"]
    if data.get("jobLocationType") == "TELECOMMUTE":
        loc_str = "Remote" if not loc_str else f"{loc_str} / Remote"

    company = org.get("name", "") if isinstance(org, dict) else str(org)
    title = data.get("title", "")
    job_id = _extract_job_id(url, data)

    posted_at = None
    if date_str := data.get("datePosted"):
        try:
            posted_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    return {
        "company_job_id": job_id,
        "company_name": company,
        "job_title": title,
        "location": loc_str or None,
        "level": _infer_level(title),
        "url": url,
        "source": "brave_search",
        "description": data.get("description", "")[:2000],
        "posted_at": posted_at,
    }


def _extract_job_id(url: str, data: dict) -> str:
    # 1. JSON-LD identifier
    ident = data.get("identifier", {})
    if isinstance(ident, dict) and ident.get("value"):
        return str(ident["value"])
    # 2. URL path segment
    match = re.search(r"/(?:jobs|positions|careers|openings|requisitions?)/([a-zA-Z0-9_-]{4,})", url)
    if match:
        return match.group(1)
    # 3. URL query param
    match = re.search(r"[?&](?:id|jobId|req|requisitionId)=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    # 4. Content hash fallback
    company = data.get("hiringOrganization", {})
    company = company.get("name", "") if isinstance(company, dict) else ""
    title = data.get("title", "")
    return hashlib.sha256(f"{company}{title}{url}".encode()).hexdigest()[:16]


def _infer_level(title: str) -> Optional[str]:
    title_lower = title.lower()
    if any(w in title_lower for w in ["staff", "principal", "distinguished"]):
        return "Staff/Principal"
    if any(w in title_lower for w in ["senior", "sr.", "sr "]):
        return "Senior"
    if any(w in title_lower for w in ["junior", "jr.", "jr ", "associate"]):
        return "Junior"
    if any(w in title_lower for w in ["lead", "tech lead"]):
        return "Lead"
    if re.search(r"\bL[3-7]\b|\bIC[3-7]\b", title):
        m = re.search(r"\bL([3-7])\b|\bIC([3-7])\b", title)
        return f"L{m.group(1) or m.group(2)}"
    return None
