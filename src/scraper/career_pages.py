"""
Direct scrapers for companies using common ATS platforms.
Greenhouse, Lever, and Ashby expose clean JSON APIs — no HTML parsing needed.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Company registries by ATS platform ───────────────────────────────────────

GREENHOUSE_COMPANIES = [
    # Big tech / infra
    "stripe", "databricks", "datadog", "confluent", "hashicorp", "cloudflare",
    "twilio", "sendgrid", "pagerduty", "newrelic",
    # Fintech
    "robinhood", "plaid", "chime", "affirm", "carta", "brex", "ramp",
    # AI / ML
    "anthropic", "cohere", "scale", "mistral",
    # Productivity / design
    "notion", "figma", "airtable", "asana", "dropbox", "canva",
    # Other
    "openai", "lyft", "doordash", "coinbase", "reddit",
]

LEVER_COMPANIES = [
    "netflix", "shopify",
    "netlify", "vercel", "supabase",
    "linear", "loom", "descript", "retool",
    "segment", "mixpanel", "amplitude", "heap",
    "benchling", "tempus", "devoted",
]

# Ashby is widely used by YC and growth-stage startups
ASHBY_COMPANIES = [
    "openai",        # also on Ashby
    "perplexity",
    "anduril",
    "benchling",
    "ramp",
    "modern-treasury",
    "watershed",
    "pilot",
    "brex",
    "mercury",
    "rippling",
    "deel",
    "gusto",
    "lattice",
    "coda",
    "product-hunt",
    "fivetran",
    "airbyte",
    "census",
    "hightouch",
    "metabase",
    "preset",
    "dbt-labs",
    "elementary-data",
]


# ── Level keywords for filtering ──────────────────────────────────────────────

# Maps user-facing level terms → keywords to look for in job titles
LEVEL_KEYWORDS = {
    "junior":      ["junior", "jr.", "jr ", "entry", "associate", "entry-level"],
    "new grad":    ["new grad", "university grad", "fresh grad", "early career",
                    "entry level", "entry-level", "0-1 year", "0-2 year", "recent grad"],
    "mid":         ["mid-level", "mid level", "software engineer ii", "engineer ii"],
    "senior":      ["senior", "sr.", "sr "],
    "staff":       ["staff", "principal", "distinguished"],
    "lead":        ["lead", "tech lead", "engineering lead"],
    "manager":     ["manager", "engineering manager", "em "],
    "director":    ["director"],
}


# ── Main entry point ──────────────────────────────────────────────────────────

async def scrape_all(titles: list[str], locations: list[str], levels: list[str] = None) -> list[dict]:
    levels = levels or []
    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for company in GREENHOUSE_COMPANIES:
            try:
                jobs = await _scrape_greenhouse(client, company, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Greenhouse {company}: {e}")

        for company in LEVER_COMPANIES:
            try:
                jobs = await _scrape_lever(client, company, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Lever {company}: {e}")

        for company in ASHBY_COMPANIES:
            try:
                jobs = await _scrape_ashby(client, company, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Ashby {company}: {e}")

    logger.info(f"Career pages: found {len(results)} matching jobs")
    return results


# ── Per-platform scrapers ─────────────────────────────────────────────────────

async def _scrape_greenhouse(client, company, titles, locations, levels):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()

    results = []
    for job in data.get("jobs", []):
        loc = job.get("location", {}).get("name", "")
        if not _matches_criteria(job.get("title", ""), loc, titles, locations, levels):
            continue
        posted_at = _parse_iso(job.get("updated_at"))
        results.append({
            "company_job_id": str(job["id"]),
            "company_name": data.get("name", company.title()),
            "job_title": job["title"],
            "location": loc or None,
            "level": _infer_level(job["title"]),
            "url": job["absolute_url"],
            "source": f"greenhouse:{company}",
            "description": _strip_html(job.get("content", ""))[:2000],
            "posted_at": posted_at,
        })
    return results


async def _scrape_lever(client, company, titles, locations, levels):
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    jobs = resp.json()

    results = []
    for job in jobs:
        loc = job.get("categories", {}).get("location", "")
        if not _matches_criteria(job.get("text", ""), loc, titles, locations, levels):
            continue
        posted_at = None
        if ts := job.get("createdAt"):
            try:
                posted_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                pass
        results.append({
            "company_job_id": job["id"],
            "company_name": company.title(),
            "job_title": job["text"],
            "location": loc or None,
            "level": _infer_level(job["text"]),
            "url": job["hostedUrl"],
            "source": f"lever:{company}",
            "description": _strip_html(job.get("descriptionBody", ""))[:2000],
            "posted_at": posted_at,
        })
    return results


async def _scrape_ashby(client, company, titles, locations, levels):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
    resp = await client.get(url)
    if resp.status_code in (404, 422):
        return []
    resp.raise_for_status()
    data = resp.json()

    results = []
    for job in data.get("jobPostings", []):
        if not job.get("isListed", True):
            continue
        loc = job.get("locationName", "") or ""
        if not _matches_criteria(job.get("title", ""), loc, titles, locations, levels):
            continue
        posted_at = _parse_iso(job.get("publishedDate"))
        results.append({
            "company_job_id": job["id"],
            "company_name": data.get("organizationName", company.title()),
            "job_title": job["title"],
            "location": loc or None,
            "level": _infer_level(job["title"]),
            "url": job["jobUrl"],
            "source": f"ashby:{company}",
            "description": _strip_html(job.get("descriptionHtml", ""))[:2000],
            "posted_at": posted_at,
        })
    return results


# ── Criteria matching ─────────────────────────────────────────────────────────

def _matches_criteria(title: str, location: str, titles: list[str],
                      locations: list[str], levels: list[str]) -> bool:
    # Title must match one of the user's target titles
    title_match = not titles or any(t.lower() in title.lower() for t in titles)
    if not title_match:
        return False

    # Location: only match if one of the user's locations appears in the job's location.
    # "remote" is NOT auto-passed — user must explicitly add "Remote" to their locations.
    location_match = not locations or any(
        loc.lower() in location.lower() for loc in locations
    )
    if not location_match:
        return False

    # Level: if user specified levels, only include jobs whose inferred level
    # matches. Jobs with NO detectable level pass through (might be unlabeled entry-level).
    if levels:
        inferred = _infer_level(title)
        if inferred is not None:
            user_levels_lower = [l.lower() for l in levels]
            if inferred.lower() not in user_levels_lower:
                return False

    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_level(title: str) -> Optional[str]:
    t = title.lower()
    if any(w in t for w in ["staff", "principal", "distinguished"]):
        return "Staff/Principal"
    if any(w in t for w in ["senior", "sr.", "sr "]):
        return "Senior"
    if any(w in t for w in ["lead", "tech lead"]):
        return "Lead"
    if any(w in t for w in ["manager", " em ", "eng manager"]):
        return "Manager"
    if any(w in t for w in ["junior", "jr.", "jr ", "associate"]):
        return "Junior"
    if any(w in t for w in ["new grad", "university grad", "entry level", "entry-level", "recent grad"]):
        return "New Grad"
    if re.search(r"\bL[3-7]\b|\bIC[3-7]\b", title):
        m = re.search(r"\bL([3-7])\b|\bIC([3-7])\b", title)
        return f"L{m.group(1) or m.group(2)}"
    return None


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
