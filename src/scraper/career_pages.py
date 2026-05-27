"""
Direct scrapers for companies using common ATS platforms.
Greenhouse, Lever, Ashby expose clean JSON APIs.
Workday uses a consistent POST JSON API.
LinkedIn uses a semi-public guest search endpoint.
"""
import hashlib
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
    "twilio", "sendgrid", "pagerduty", "newrelic", "github", "airbnb",
    "box", "lyft", "doordash", "coinbase", "reddit", "pinterest",
    # Fintech
    "robinhood", "plaid", "chime", "affirm", "carta", "brex", "ramp",
    # AI / ML
    "anthropic", "cohere", "scale", "mistral",
    # Productivity / design
    "notion", "figma", "airtable", "asana", "dropbox", "canva",
    # Other
    "openai", "waymo", "zendesk", "instacart",
]

LEVER_COMPANIES = [
    "netflix", "shopify",
    "netlify", "vercel", "supabase",
    "linear", "loom", "descript", "retool",
    "segment", "mixpanel", "amplitude", "heap",
    "benchling", "tempus", "devoted",
    "palantir",
]

ASHBY_COMPANIES = [
    "openai",
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
    "fivetran",
    "airbyte",
    "census",
    "hightouch",
    "metabase",
    "preset",
    "dbt-labs",
    "elementary-data",
]

# (tenant, board_path) — Workday URL = https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
WORKDAY_COMPANIES = [
    ("salesforce",   "Salesforce",          "External_Career_Site"),
    ("nvidia",       "NVIDIA",              "NVIDIAExternalCareerSite"),
    ("adobe",        "Adobe",               "external_experienced"),
    ("uber-temp",    "Uber",                "Uber_External_Careers"),
    ("snowflake-ext","Snowflake",           "Snowflake_External_Career"),
    ("zoom",         "Zoom",                "Zoom"),
    ("okta",         "Okta",                "okta"),
    ("linkedin",     "LinkedIn",            "careers"),
    ("block",        "Block (Square)",      "External"),
    ("twitch",       "Twitch",              "Careers"),
    ("unity-technologies", "Unity",         "Jobs"),
    ("pinterest",    "Pinterest",           "External"),
    ("splunk",       "Splunk",              "External"),
    ("workday",      "Workday",             "workday"),
    ("intuit",       "Intuit",              "CareerSite"),
    ("cisco",        "Cisco",               "Careers"),
]


# Career domains that have dedicated scrapers — used to skip re-registering them as "custom"
KNOWN_CAREER_DOMAINS = {
    "careers.google.com",
    "metacareers.com",
    "jobs.apple.com",
    "amazon.jobs",
}


# ── Level keywords for filtering ──────────────────────────────────────────────

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

    # Load any companies the user has manually taught us about
    db_companies = _load_db_companies()

    async with httpx.AsyncClient(timeout=15) as client:
        # Greenhouse
        gh_slugs = set(GREENHOUSE_COMPANIES) | {c["ats_slug"] for c in db_companies if c["ats_type"] == "greenhouse"}
        for company in gh_slugs:
            try:
                jobs = await _scrape_greenhouse(client, company, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Greenhouse {company}: {e}")

        # Lever
        lv_slugs = set(LEVER_COMPANIES) | {c["ats_slug"] for c in db_companies if c["ats_type"] == "lever"}
        for company in lv_slugs:
            try:
                jobs = await _scrape_lever(client, company, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Lever {company}: {e}")

        # Ashby
        ab_slugs = set(ASHBY_COMPANIES) | {c["ats_slug"] for c in db_companies if c["ats_type"] == "ashby"}
        for company in ab_slugs:
            try:
                jobs = await _scrape_ashby(client, company, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Ashby {company}: {e}")

        # Workday
        wd_companies = list(WORKDAY_COMPANIES)
        for c in db_companies:
            if c["ats_type"] == "workday" and c.get("workday_board"):
                wd_companies.append((c["ats_slug"], c["company_name"], c["workday_board"]))
        for tenant, display_name, board in wd_companies:
            try:
                jobs = await _scrape_workday(client, tenant, display_name, board, titles, locations, levels)
                results.extend(jobs)
            except Exception as e:
                logger.debug(f"Workday {tenant}: {e}")

        # LinkedIn
        try:
            jobs = await _scrape_linkedin(client, titles, locations, levels)
            results.extend(jobs)
        except Exception as e:
            logger.debug(f"LinkedIn: {e}")

        # Google Careers (always included — major Bay Area employer)
        try:
            jobs = await _scrape_google(client, titles, locations, levels)
            results.extend(jobs)
        except Exception as e:
            logger.debug(f"Google Careers: {e}")

        # Custom career sites learned from manual adds (best-effort HTML scrape)
        for c in db_companies:
            if c["ats_type"] == "custom" and c.get("career_url"):
                try:
                    jobs = await _scrape_custom_site(client, c["company_name"], c["career_url"], titles, locations, levels)
                    results.extend(jobs)
                except Exception as e:
                    logger.debug(f"Custom {c['company_name']}: {e}")

    logger.info(f"Career pages: found {len(results)} matching jobs")
    return results


def _load_db_companies() -> list[dict]:
    try:
        from src.api.database import SessionLocal
        from src.api.models import TrackedCompany
        with SessionLocal() as db:
            rows = db.query(TrackedCompany).filter_by(is_active=True).all()
            return [
                {
                    "company_name": r.company_name,
                    "ats_type": r.ats_type,
                    "ats_slug": r.ats_slug,
                    "workday_board": r.workday_board,
                    "career_url": r.career_url,
                }
                for r in rows
            ]
    except Exception as e:
        logger.debug(f"Could not load tracked companies from DB: {e}")
        return []


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
        results.append({
            "company_job_id": str(job["id"]),
            "company_name": data.get("name", company.title()),
            "job_title": job["title"],
            "location": loc or None,
            "level": _infer_level(job["title"]),
            "url": job["absolute_url"],
            "source": f"greenhouse:{company}",
            "description": _strip_html(job.get("content", ""))[:2000],
            "posted_at": _parse_iso(job.get("updated_at")),
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
        results.append({
            "company_job_id": job["id"],
            "company_name": data.get("organizationName", company.title()),
            "job_title": job["title"],
            "location": loc or None,
            "level": _infer_level(job["title"]),
            "url": job["jobUrl"],
            "source": f"ashby:{company}",
            "description": _strip_html(job.get("descriptionHtml", ""))[:2000],
            "posted_at": _parse_iso(job.get("publishedDate")),
        })
    return results


async def _scrape_workday(client, tenant, display_name, board, titles, locations, levels):
    url = f"https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    }
    # Search for each title combination
    results = []
    seen = set()
    for title_kw in (titles or [""]):
        body = {"limit": 20, "offset": 0, "searchText": title_kw}
        try:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code in (404, 403, 422):
                return []
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        for job in data.get("jobPostings", []):
            job_id = job.get("bulletFields", [None])[0] or job.get("externalPath", "")
            if job_id in seen:
                continue
            seen.add(job_id)

            title = job.get("title", "")
            loc = job.get("locationsText", "")
            if not _matches_criteria(title, loc, titles, locations, levels):
                continue

            path = job.get("externalPath", "")
            job_url = f"https://{tenant}.wd5.myworkdayjobs.com{path}" if path.startswith("/") else path
            posted_at = _parse_iso(job.get("postedOn"))

            results.append({
                "company_job_id": path.split("/")[-1] if path else job_id,
                "company_name": display_name,
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": job_url,
                "source": f"workday:{tenant}",
                "description": None,
                "posted_at": posted_at,
            })
    return results


async def _scrape_linkedin(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """Use LinkedIn's guest job search API (no auth needed, public embed endpoint)."""
    results = []
    seen = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/",
    }

    # Map our location names to LinkedIn geo URNs for Bay Area
    location_param = "San Francisco Bay Area" if locations else ""

    for title_kw in (titles or []):
        url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        params = {
            "keywords": title_kw,
            "location": location_param,
            "start": "0",
            "f_TPR": "r604800",  # past week
        }
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                continue
        except Exception:
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        for card in soup.select("li"):
            link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
            if not link_el:
                continue
            job_url = link_el.get("href", "").split("?")[0]
            if not job_url or job_url in seen:
                continue
            seen.add(job_url)

            title_el = card.select_one(".base-search-card__title, h3")
            company_el = card.select_one(".base-search-card__subtitle, h4")
            loc_el = card.select_one(".job-search-card__location")
            time_el = card.select_one("time")

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else "Unknown"
            loc = loc_el.get_text(strip=True) if loc_el else ""

            if not title or not _matches_criteria(title, loc, titles, locations, levels):
                continue

            posted_at = None
            if time_el and time_el.get("datetime"):
                posted_at = _parse_iso(time_el["datetime"])

            job_id_match = re.search(r"/jobs/view/(\d+)", job_url)
            job_id = job_id_match.group(1) if job_id_match else job_url[-20:]

            results.append({
                "company_job_id": f"li_{job_id}",
                "company_name": company,
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": job_url,
                "source": "linkedin",
                "description": None,
                "posted_at": posted_at,
            })

    return results


async def _scrape_google(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """Google Careers public API — no auth needed."""
    results = []
    seen = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    }
    for title_kw in (titles or [""]):
        params = {
            "q": title_kw,
            "location": "San Francisco Bay Area, CA, USA",
            "jlo": "en_US",
            "num": "20",
        }
        try:
            resp = await client.get(
                "https://careers.google.com/api/v3/search/",
                params=params, headers=headers,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for job in data.get("jobs", []):
            job_id = job.get("id", "")
            if job_id in seen:
                continue
            seen.add(job_id)

            title = job.get("title", "")
            # locations is a list of dicts like [{"display": "San Francisco, CA, USA"}]
            locs = job.get("locations", [])
            loc = ", ".join(l.get("display", "") for l in locs if l.get("display"))
            if not _matches_criteria(title, loc, titles, locations, levels):
                continue

            apply_url = job.get("apply_url") or f"https://careers.google.com/jobs/results/{job_id}"
            posted_at = _parse_iso(job.get("publish_date"))
            results.append({
                "company_job_id": f"google_{job_id}",
                "company_name": "Google",
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": apply_url,
                "source": "google",
                "description": job.get("description", "")[:2000],
                "posted_at": posted_at,
            })
    return results


async def _scrape_custom_site(client, company_name: str, career_url: str,
                               titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """
    Best-effort scrape of a custom career site the user taught us about.
    We do a basic fetch + look for job links/titles. Not perfect but captures obvious listings.
    """
    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        # Try common career page paths
        for path in ["/careers", "/jobs", "/careers/open-positions", ""]:
            try:
                resp = await client.get(career_url + path, headers=headers, follow_redirects=True)
                if resp.status_code == 200 and len(resp.text) > 500:
                    break
            except Exception:
                continue
        else:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        # Look for job links — any <a> whose text looks like a job title
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text or len(text) < 5 or len(text) > 120:
                continue
            if not _matches_criteria(text, "", titles, [], levels):
                continue
            href = a["href"]
            if not href.startswith("http"):
                href = career_url.rstrip("/") + "/" + href.lstrip("/")
            results.append({
                "company_job_id": hashlib.md5(href.encode()).hexdigest()[:16],
                "company_name": company_name,
                "job_title": text,
                "location": None,
                "level": _infer_level(text),
                "url": href,
                "source": f"custom:{career_url}",
                "description": None,
                "posted_at": None,
            })
            if len(results) >= 20:
                break
    except Exception as e:
        logger.debug(f"Custom site scrape {career_url}: {e}")
    return results


# ── Criteria matching ─────────────────────────────────────────────────────────

def _matches_criteria(title: str, location: str, titles: list[str],
                      locations: list[str], levels: list[str]) -> bool:
    title_match = not titles or any(t.lower() in title.lower() for t in titles)
    if not title_match:
        return False

    location_match = not locations or any(
        loc.lower() in location.lower() for loc in locations
    )
    if not location_match:
        return False

    if levels:
        inferred = _infer_level(title)
        if inferred is not None:
            user_levels_lower = [l.lower() for l in levels]
            if inferred.lower() not in user_levels_lower:
                return False

    return True


# ── ATS detection (used when user manually adds a job) ───────────────────────

def detect_ats_from_url(url: str) -> Optional[dict]:
    """
    Given a job posting URL, return {'ats_type', 'ats_slug', 'workday_board'} or None.
    Called when a user manually adds a job so we can learn the company's ATS.
    """
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.netloc.lower()
        path = p.path

        if "greenhouse.io" in host:
            # boards.greenhouse.io/v1/boards/{slug}/... or boards.greenhouse.io/{slug}/jobs/...
            m = re.search(r"/boards/([^/]+)/", path) or re.search(r"^/([^/]+)/jobs", path)
            slug = m.group(1) if m else None
            if slug:
                return {"ats_type": "greenhouse", "ats_slug": slug, "workday_board": None}

        if "lever.co" in host:
            m = re.match(r"/([^/]+)/", path)
            slug = m.group(1) if m else None
            if slug:
                return {"ats_type": "lever", "ats_slug": slug, "workday_board": None}

        if "ashbyhq.com" in host:
            m = re.match(r"/([^/]+)/", path)
            slug = m.group(1) if m else None
            if slug:
                return {"ats_type": "ashby", "ats_slug": slug, "workday_board": None}

        if "myworkdayjobs.com" in host:
            # host: {tenant}.wd5.myworkdayjobs.com
            tenant = host.split(".")[0]
            # path: /wday/cxs/{tenant}/{board}/... or /{board}/job/...
            parts = [p for p in path.split("/") if p]
            board = parts[0] if parts else tenant
            return {"ats_type": "workday", "ats_slug": tenant, "workday_board": board}

    except Exception:
        pass
    return None


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
    if any(w in t for w in ["director"]):
        return "Director"
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
