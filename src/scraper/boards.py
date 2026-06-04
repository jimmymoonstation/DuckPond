"""
Additional job board scrapers using DDG site-scoped search.
Wellfound and YC Work at a Startup both block server-side scraping,
so we use DuckDuckGo `site:` queries to discover their job listings.
"""
import hashlib
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)


def _job_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def _ddg(query: str, max_results: int = 20) -> list[dict]:
    import time
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        time.sleep(1.5)
        return results
    except Exception as e:
        logger.debug(f"DDG search failed for '{query}': {e}")
        return []


# ── Wellfound (wellfound.com) ─────────────────────────────────────────────────

async def _scrape_wellfound(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """
    Search Wellfound startup jobs via DDG site: queries.
    Wellfound blocks direct HTTP scraping (403), so we use DuckDuckGo
    to find individual job pages indexed on wellfound.com.
    """
    from src.scraper.career_pages import _matches_criteria, _infer_level
    from src.scraper.web_search import _clean_title, _title_from_url, _is_individual_job_url
    import asyncio

    results = []
    seen: set[str] = set()
    loc_kw = '"San Francisco" OR "Bay Area" OR "Remote"'

    for title_kw in (titles or [])[:4]:
        query = f'"{title_kw}" {loc_kw} site:wellfound.com'
        raw = await asyncio.get_event_loop().run_in_executor(None, _ddg, query, 15)
        await asyncio.sleep(1)

        for r in raw:
            url  = r.get("href", "")
            snip = r.get("body", "")

            if not url or "wellfound.com" not in url:
                continue
            if not _is_individual_job_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)

            title    = _title_from_url(url) or _clean_title(r.get("title", ""))
            # Try extracting company from URL: wellfound.com/company/{slug}/jobs/{id}
            m = re.search(r"wellfound\.com/(?:company/([^/?#]+)/jobs|jobs/([^/?#]+))", url)
            company  = (m.group(1) or m.group(2) or "").replace("-", " ").title() if m else ""
            location = ""
            if "remote" in snip.lower():
                location = "Remote"
            elif "san francisco" in snip.lower() or "bay area" in snip.lower():
                location = "San Francisco Bay Area"

            if not title or not _matches_criteria(title, location or snip, titles, locations, levels):
                continue

            results.append({
                "company_job_id": _job_id(url),
                "company_name":   company or "Unknown",
                "job_title":      title,
                "location":       location or None,
                "level":          _infer_level(title),
                "url":            url,
                "source":         "wellfound",
                "description":    snip[:500] or None,
                "posted_at":      None,
            })

    logger.info(f"Wellfound (DDG): {len(results)} matching jobs")
    return results


# ── Y Combinator Work at a Startup ───────────────────────────────────────────

async def _scrape_yc(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """
    Y Combinator Work at a Startup — direct JSON API.
    Endpoint: https://www.workatastartup.com/jobs/search?q=QUERY
    Returns structured data including YC batch, company, salary, location.
    """
    from src.scraper.career_pages import _matches_criteria, _infer_level
    import asyncio

    # Location keywords accepted by the WAAS search
    _US_LOC_TERMS = {"san francisco", "bay area", "new york", "seattle",
                     "los angeles", "austin", "boston", "chicago", "remote"}

    results = []
    seen: set[str] = set()

    for title_kw in (titles or [])[:4]:
        try:
            resp = await client.get(
                "https://www.workatastartup.com/jobs/search",
                params={"q": title_kw, "jobType": "fulltime"},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                },
                timeout=12,
            )
            if resp.status_code != 200:
                logger.warning(f"YC API {resp.status_code} for '{title_kw}'")
                continue
            jobs = resp.json().get("jobs", [])
        except Exception as e:
            logger.warning(f"YC API error: {e}")
            continue

        await asyncio.sleep(1)

        for job in jobs:
            job_id   = str(job.get("id", ""))
            title    = (job.get("title") or "").strip()
            company  = (job.get("companyName") or "").strip()
            loc_raw  = (job.get("location") or "").strip()
            batch    = (job.get("companyBatch") or "").strip()      # "W24", "S21", etc.
            one_liner = (job.get("companyOneLiner") or "").strip()
            salary   = job.get("salary") or ""
            slug     = job.get("companySlug", "")

            if not title or not company or not job_id:
                continue

            url = f"https://www.workatastartup.com/jobs/{job_id}"
            if url in seen:
                continue
            seen.add(url)

            # Location filter — keep US/Remote only
            loc_lower = loc_raw.lower()
            is_us_or_remote = (
                "remote" in loc_lower
                or any(t in loc_lower for t in _US_LOC_TERMS)
                or loc_lower in {"us", "united states"}
                or loc_lower == ""
            )
            if not is_us_or_remote:
                continue

            # Title / criteria filter
            if not _matches_criteria(title, loc_raw, titles, locations, levels):
                continue

            # Build description with YC metadata
            desc_parts = []
            if batch:
                desc_parts.append(f"YC {batch}")
            if one_liner:
                desc_parts.append(one_liner)
            if salary:
                desc_parts.append(f"Salary: {salary}")
            description = " · ".join(desc_parts) if desc_parts else None

            # tags: "yc" + batch if available
            tags_list = ["startup", "yc"]
            if batch:
                tags_list.append(batch.lower())
            import json as _json
            tags = _json.dumps(tags_list)

            results.append({
                "company_job_id": f"yc_{job_id}",
                "company_name":   company,
                "job_title":      title,
                "location":       loc_raw or None,
                "level":          _infer_level(title),
                "url":            url,
                "source":         "yc",
                "description":    description,
                "posted_at":      None,
                "tags":           tags,
            })

    logger.info(f"YC Work at a Startup (API): {len(results)} matching jobs")
    return results
