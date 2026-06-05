"""
Web search–based job discovery using DuckDuckGo.

Two main functions:
  scrape_via_web_search  — finds specific job links for a known custom-ATS company
  discover_new_companies — searches standard ATS board domains to find new companies
                           we're not yet tracking
"""
import asyncio
import hashlib
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

# Domains that are job aggregators / boards, never actual employer career pages
_AGGREGATOR_DOMAINS = {
    "jobright.ai", "dice.com", "ziprecruiter.com", "zippia.com",
    "builtinsf.com", "builtinnyc.com", "builtin.com",
    "simplyhired.com", "monster.com", "careerbuilder.com",
    "getwork.com", "jobvite.com", "talent.com", "jobisjob.com",
    "jobtome.com", "neuvoo.com", "adzuna.com", "jobsora.com",
    "glassdoor.com", "indeed.com", "linkedin.com",
}

# URL path patterns that indicate a search/category page, not an individual job
_SEARCH_PATH_RE = re.compile(
    r"(^/$"
    r"|/jobs/?$"
    r"|/careers/?$"
    r"|/search"
    r"|/c/"                              # Snowflake-style category pages
    r"|/category/"
    r"|/browse"
    r"|-jobs-in-[a-z]"                   # aggregator slug: "data-engineer-jobs-in-irvine-ca"
    r"|/jobs/[a-z][a-z0-9-]+-jobs-in-"  # same pattern deeper in path
    r")",
    re.IGNORECASE,
)


def _is_individual_job_url(url: str) -> bool:
    """Return True only if the URL looks like a specific job posting, not a search/category page."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")

    # Reject known aggregator domains entirely
    if any(host == d or host.endswith("." + d) for d in _AGGREGATOR_DOMAINS):
        return False

    path = parsed.path.rstrip("/")
    if _SEARCH_PATH_RE.search(path):
        return False

    # Require at least two path segments (e.g. /jobs/1234, not just /careers)
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return False

    return True


_NOISE_TITLE_SUFFIXES = re.compile(
    r"\s*[\|\-–—]\s*(careers?|jobs?|hiring|employment|apply|apply now|"
    r"linkedin|indeed|glassdoor|zippia|ziprecruiter|monster|built ?in|"
    r"lever|greenhouse|ashby|workday).*$",
    re.IGNORECASE,
)
_AT_COMPANY = re.compile(r"\s+(at|@)\s+[\w &,]+$", re.IGNORECASE)


def _clean_title(raw: str, company_name: str = "") -> str:
    """Strip company/site names from a DDG result title to get a clean job title."""
    t = _NOISE_TITLE_SUFFIXES.sub("", raw).strip()
    t = _AT_COMPANY.sub("", t).strip()
    if company_name:
        for cname in [company_name, company_name.lower()]:
            if t.lower().startswith(cname.lower()):
                t = t[len(cname):].lstrip(" :-–|").strip()
            if t.lower().endswith(cname.lower()):
                t = t[: -len(cname)].rstrip(" :-–|@").strip()
    return t or raw.strip()


def _title_from_url(url: str) -> str:
    """
    Many career sites embed the job title in the URL slug, e.g.:
      careers.google.com/jobs/results/123-senior-data-engineer-remote/
      metacareers.com/jobs/data-engineer-platform-12345/
    Extract it by taking the last path segment and cleaning up.
    Returns "" if the slug looks like a search/category page rather than a job.
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip("/")
    last = path.split("/")[-1] if path else ""
    # Strip leading/trailing numeric IDs like "12345-" or "-12345"
    last = re.sub(r"^\d+-", "", last)
    last = re.sub(r"-\d+$", "", last)
    last = re.sub(r"_\d+$", "", last)
    if not last or len(last) < 5:
        return ""
    # Reject search-page slugs: "data-engineer-jobs-in-irvine-ca"
    if re.search(r"-jobs-in-[a-z]", last, re.IGNORECASE):
        return ""
    # Reject category slugs: "data-and-analytics", "data-analytics-and-ai-jobs"
    if re.search(r"\band\b.*\bjobs?\b|\bjobs?\b.*\band\b", last, re.IGNORECASE):
        return ""
    return last.replace("-", " ").replace("_", " ").title()


def _snippet_location(snippet: str, locations: list[str]) -> str:
    """Try to find a location hint inside a DDG snippet."""
    for loc in locations:
        if loc.lower() in snippet.lower():
            return loc
    # Common patterns: "San Francisco, CA", "Remote", etc.
    m = re.search(
        r"\b(remote|san francisco|new york|seattle|austin|los angeles|chicago|boston|"
        r"bay area|new york city|nyc|sf|ca|wa|ny)\b",
        snippet, re.IGNORECASE,
    )
    return m.group(0).title() if m else ""


def _make_job_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]


_ddg_failures = 0        # consecutive failure counter
_DDG_FAILURE_LIMIT = 3   # trip the breaker after this many consecutive timeouts


def reset_ddg_circuit():
    global _ddg_failures
    _ddg_failures = 0


def _get_proxy() -> str | None:
    import os
    return os.getenv("WEB_SEARCH_PROXY") or None


def _ddg_text(query: str, max_results: int = 15) -> list[dict]:
    """Synchronous DDG text search with proxy support and circuit breaker."""
    global _ddg_failures
    import time

    if _ddg_failures >= _DDG_FAILURE_LIMIT:
        return []

    try:
        from ddgs import DDGS
        proxy = _get_proxy()
        with DDGS(proxy=proxy) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        _ddg_failures = 0
        time.sleep(0.5)
        if proxy and results is not None:
            from src.api.usage import record_webshare
            record_webshare(calls=1)
        return results
    except Exception as e:
        _ddg_failures += 1
        if _ddg_failures >= _DDG_FAILURE_LIMIT:
            logger.warning(f"DDG circuit breaker tripped after {_DDG_FAILURE_LIMIT} consecutive failures — skipping remaining web searches this run")
        else:
            logger.warning(f"DDG search failed for '{query}': {e}")
        return []


# ── 1. Search-based scraper for known custom-ATS companies ────────────────────

async def scrape_via_web_search(
    company_name: str,
    career_url: str,
    titles: list[str],
    locations: list[str],
    levels: list[str],
) -> list[dict]:
    """
    For a company whose career page can't be scraped via API (custom ATS),
    use DDG to find specific job postings and return job dicts.

    Strategy:
      1. site:{domain} search — finds indexed job pages with the title in query
      2. Broader search — {company} {title} careers (no site restriction)
      In both cases, title is extracted from URL slug when the DDG title is generic.
    """
    from src.scraper.career_pages import _matches_criteria
    from urllib.parse import urlparse

    domain = urlparse(career_url).netloc.lstrip("www.")
    loc_kw = " OR ".join(f'"{l}"' for l in locations[:2]) if locations else '"Bay Area" OR "remote"'

    raw_results: list[dict] = []

    for title_kw in titles[:3]:
        # Strategy 1: site-scoped search
        q1 = f'"{title_kw}" {loc_kw} site:{domain}'
        raw_results += await asyncio.get_event_loop().run_in_executor(None, _ddg_text, q1, 10)

        # Strategy 2: broader search mentioning the company + careers page
        q2 = f'{company_name} "{title_kw}" {loc_kw} careers -site:linkedin.com -site:glassdoor.com -site:indeed.com'
        r2 = await asyncio.get_event_loop().run_in_executor(None, _ddg_text, q2, 8)
        # Keep only results that point to the company's own domain
        raw_results += [r for r in r2 if domain in r.get("href", "")]

    jobs = []
    seen_urls: set[str] = set()

    for r in raw_results:
        url  = r.get("href", "")
        snip = r.get("body", "")

        if not url or domain not in url:
            continue

        # Reject search/category pages and aggregator domains
        if not _is_individual_job_url(url):
            continue

        # Prefer title from URL slug (more reliable than DDG result title)
        job_title = _title_from_url(url) or _clean_title(r.get("title", ""), company_name)
        location  = _snippet_location(snip, locations)

        if not _matches_criteria(job_title, location, titles, locations, levels):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        jobs.append({
            "company_job_id": _make_job_id(url),
            "company_name":   company_name,
            "job_title":      job_title,
            "location":       location,
            "url":            url,
            "source":         f"web_search:{company_name.lower().replace(' ', '_')}",
            "discovered_at":  datetime.utcnow(),
            "is_active":      True,
        })

    logger.info(f"WebSearch [{company_name}]: {len(jobs)} matching jobs found")
    return jobs


# ── 2. Auto-discovery of new companies on standard ATS platforms ───────────────

_ATS_SEARCH_DOMAINS = [
    # Greenhouse (two URL formats)
    ("boards.greenhouse.io",        "greenhouse",      r"boards\.greenhouse\.io/([^/?#]+)"),
    ("job-boards.greenhouse.io",    "greenhouse",      r"job-boards\.greenhouse\.io/([^/?#]+)"),
    # Lever
    ("jobs.lever.co",               "lever",           r"jobs\.lever\.co/([^/?#]+)"),
    # Ashby
    ("jobs.ashbyhq.com",            "ashby",           r"jobs\.ashbyhq\.com/([^/?#]+)"),
    # SmartRecruiters
    ("jobs.smartrecruiters.com",    "smartrecruiters", r"jobs\.smartrecruiters\.com/([^/?#]+)"),
    # Workday (matches {slug}.wd5.myworkdayjobs.com etc.)
    ("myworkdayjobs.com",           "workday",         r"([\w-]+)\.(?:wd\d+)\.myworkdayjobs\.com"),
    # Rippling ATS
    ("ats.rippling.com",            "rippling",        r"ats\.rippling\.com/([^/?#]+)"),
    # Workable
    ("apply.workable.com",          "workable",        r"apply\.workable\.com/([^/?#]+)"),
]

_NOISE_SLUGS = {
    "", "jobs", "careers", "embed", "v1", "api", "search", "apply",
    "jobright", "jobright.ai", "dice", "ziprecruiter", "builtinsf",
    "builtin", "simplyhired", "monster", "careerbuilder", "talent",
    "adzuna", "getwork", "indeed", "glassdoor", "linkedin",
}

# Focused location keywords for discovery queries
_DISCOVERY_LOCS = '"Bay Area" OR "San Francisco" OR "Remote" OR "New York" OR "Seattle"'


async def discover_new_companies(
    titles: list[str],
    locations: list[str],
) -> list[dict]:
    """Search DDG + Brave API for jobs on standard ATS boards to find untracked companies."""
    from src.api.database import SessionLocal
    from src.api.models import TrackedCompany

    db = SessionLocal()
    try:
        all_tracked = db.query(TrackedCompany).all()
        tracked_keys  = {(c.ats_type, c.ats_slug) for c in all_tracked}
        tracked_names = {c.company_name.lower().replace(" ", "").replace("-", "") for c in all_tracked}
    finally:
        db.close()

    candidates = {}
    loc_kw = _DISCOVERY_LOCS

    career_url_map = {
        "greenhouse":      "https://job-boards.greenhouse.io/{slug}",
        "lever":           "https://jobs.lever.co/{slug}",
        "ashby":           "https://jobs.ashbyhq.com/{slug}",
        "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
        "rippling":        "https://ats.rippling.com/{slug}",
        "workable":        "https://apply.workable.com/{slug}",
    }

    def _process_url(url: str):
        for domain, ats_type, slug_pattern in _ATS_SEARCH_DOMAINS:
            if domain not in url:
                continue
            m = re.search(slug_pattern, url)
            if not m:
                continue
            slug = m.group(1).lower().rstrip("/").split("/")[0]
            if slug in _NOISE_SLUGS:
                return
            key = (ats_type, slug)
            if key in tracked_keys or key in candidates:
                return
            normalized = re.sub(r"[^a-z0-9]", "", slug)
            if normalized in tracked_names:
                return
            company_name = slug.replace("-", " ").replace("_", " ").title()
            url_template = career_url_map.get(ats_type, url)
            candidates[key] = {
                "company_name":    company_name,
                "ats_type":        ats_type,
                "ats_slug":        slug,
                "career_url":      url_template.format(slug=slug),
                "discovered_from": "auto",
            }
            logger.info(f"CompanyDiscovery: found {company_name} ({ats_type}:{slug})")

    # Search DDG across all titles and ATS domains
    for title_kw in titles:  # all titles, not just first 3
        for domain, ats_type, _ in _ATS_SEARCH_DOMAINS:
            query = f'"{title_kw}" {loc_kw} site:{domain}'
            results = await asyncio.get_event_loop().run_in_executor(
                None, _ddg_text, query, 30
            )
            for r in results:
                _process_url(r.get("href", ""))

    # Also search Brave API for additional coverage
    await _brave_discover(titles, candidates, tracked_keys, tracked_names, _process_url)

    return list(candidates.values())


async def _brave_discover(titles, candidates, tracked_keys, tracked_names, process_fn):
    """Use Brave Search API to find additional untracked companies."""
    import os
    import httpx
    key = os.getenv("BRAVE_API_KEY", "")
    if not key:
        return
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for title in titles[:3]:
                for domain, _, _ in _ATS_SEARCH_DOMAINS[:6]:
                    query = f'"{title}" "Bay Area" OR "Remote" site:{domain}'
                    resp = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        headers={"Accept": "application/json", "Accept-Encoding": "gzip",
                                 "X-Subscription-Token": key},
                        params={"q": query, "count": 20},
                    )
                    if resp.status_code != 200:
                        continue
                    from src.api.usage import record_brave
                    record_brave(calls=1)
                    for result in resp.json().get("web", {}).get("results", []):
                        process_fn(result.get("url", ""))
    except Exception as e:
        logger.debug(f"Brave discovery error: {e}")


# ── 3. YC company discovery ────────────────────────────────────────────────────

async def discover_yc_companies() -> list[dict]:
    """Scrape the YC company directory and probe each for a known ATS."""
    import httpx
    from src.api.database import SessionLocal
    from src.api.models import TrackedCompany

    db = SessionLocal()
    try:
        tracked_names = {c.company_name.lower().replace(" ", "").replace("-", "")
                         for c in db.query(TrackedCompany).all()}
    finally:
        db.close()

    new_companies = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.ycombinator.com/v0.1/companies?batch=W25&batch=S25&batch=W24&batch=S24",
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return []
            companies = resp.json().get("companies", [])
            logger.info(f"YC discovery: found {len(companies)} YC companies to probe")
            for co in companies[:60]:  # probe up to 60 per run
                name = co.get("name", "")
                if not name:
                    continue
                norm = name.lower().replace(" ", "").replace("-", "")
                if norm in tracked_names:
                    continue
                from src.api.routes.companies import _auto_probe
                probe = asyncio.run(_auto_probe(name)) if not asyncio.get_event_loop().is_running() else await _auto_probe_async(name, client)
                if probe:
                    new_companies.append({
                        "company_name":    name,
                        "ats_type":        probe["ats_type"],
                        "ats_slug":        probe["ats_slug"],
                        "career_url":      co.get("url", ""),
                        "discovered_from": "yc",
                    })
                    tracked_names.add(norm)
    except Exception as e:
        logger.warning(f"YC discovery failed: {e}")
    return new_companies


async def _auto_probe_async(company_name: str, client) -> dict | None:
    """Async version of company ATS probe."""
    from src.api.routes.companies import _auto_probe
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: asyncio.run(_auto_probe(company_name)))


# ── 4. Orchestrator called by the scheduler ───────────────────────────────────

async def run_company_discovery() -> dict:
    """Full discovery run: DDG + Brave + YC → find new companies → add to DB."""
    import json
    from src.api.database import SessionLocal
    from src.api.models import SearchConfig, TrackedCompany

    db = SessionLocal()
    try:
        cfg = db.query(SearchConfig).filter_by(is_active=True).first()
        titles    = json.loads(cfg.titles_json    or "[]") if cfg else []
        locations = json.loads(cfg.locations_json or "[]") if cfg else []
    finally:
        db.close()

    if not titles:
        logger.info("CompanyDiscovery: no titles configured, skipping")
        return {"new": 0, "skipped": 0}

    # Run DDG + Brave discovery
    candidates = await discover_new_companies(titles, locations)

    # Also run YC discovery
    yc_candidates = await discover_yc_companies()
    candidates.extend(yc_candidates)

    added = skipped = 0
    db = SessionLocal()
    try:
        existing_names = {
            re.sub(r"[^a-z0-9]", "", c.company_name.lower())
            for c in db.query(TrackedCompany).all()
        }
        for c in candidates[:50]:   # increased cap: 30 → 50
            existing = db.query(TrackedCompany).filter_by(
                ats_type=c["ats_type"], ats_slug=c["ats_slug"]
            ).first()
            if existing:
                skipped += 1
                continue
            norm = re.sub(r"[^a-z0-9]", "", c["ats_slug"])
            if norm in existing_names:
                skipped += 1
                continue
            db.add(TrackedCompany(
                company_name=c["company_name"],
                ats_type=c["ats_type"],
                ats_slug=c["ats_slug"],
                career_url=c.get("career_url", ""),
                discovered_from=c.get("discovered_from", "auto"),
                is_active=True,
            ))
            existing_names.add(norm)
            added += 1
        db.commit()
    finally:
        db.close()

    logger.info(f"CompanyDiscovery: added {added} new, {skipped} already tracked (from {len(candidates)} candidates)")
    return {"new": added, "skipped": skipped, "candidates": len(candidates)}
