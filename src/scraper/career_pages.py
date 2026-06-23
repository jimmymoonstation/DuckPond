"""
Direct scrapers for companies using common ATS platforms.
Greenhouse, Lever, Ashby expose clean JSON APIs.
Workday uses a consistent POST JSON API.
LinkedIn uses a semi-public guest search endpoint.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
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
    "marqeta", "nerdwallet", "blend", "lob", "finix",
    # AI / ML
    "anthropic", "cohere", "scale", "mistral", "adept",
    # Productivity / design
    "notion", "figma", "airtable", "asana", "dropbox", "canva", "miro",
    # Data / infrastructure
    "elastic", "mongodb", "couchbase", "redis", "cockroachdb",
    "algolia", "contentful", "snyk",
    # Analytics / observability
    "sentry", "grafana", "chronosphere",
    # Security
    "zscaler", "lacework", "orca-security",
    # Other Bay Area tech
    "uber", "unity3d", "gitlab", "discord", "twitch", "duolingo",
    "intercom", "fastly", "okta", "veeva", "nuro",
    "coursera", "faire", "samsara", "opendoor",
    "zendesk", "instacart", "waymo", "openai",
    # Healthcare tech
    "hims", "virta-health",
    # E-commerce / logistics
    "shiptify", "shippo",
]

LEVER_COMPANIES = [
    "netflix", "shopify",
    "netlify", "supabase",
    "linear", "loom", "descript", "retool",
    "segment", "mixpanel", "amplitude", "heap",
    "benchling", "tempus", "devoted",
    "palantir", "grammarly", "flexport",
    # Additional
    "scaleai", "remote", "watershed-climate",
    "opentable", "yelp", "eventbrite",
    "cloudkitchens", "getaround",
    "easypost", "shipbob",
    "brainly", "duolingo",
]

# (company_id, display_name) for SmartRecruiters
SMARTRECRUITERS_COMPANIES = [
    ("servicenow",          "ServiceNow"),
    ("paloaltonetworks2",   "Palo Alto Networks"),
    ("Square",              "Block"),
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
    # Additional data / infra tooling
    "hex",
    "dagsterdata",
    "prefect",
    "modal",
    "together",
    "runway",
    "vercel",
    "linear",
    "luma",
    "replit",
    "cursor",
    "sourcegraph",
    "weaviate",
    "qdrant",
    "chroma",
    "pinecone",
]

# (tenant, wd_version, display_name, board_path)
# URL pattern: https://{tenant}.{wd_version}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
WORKDAY_COMPANIES = [
    ("salesforce",          "wd12", "Salesforce",          "External_Career_Site"),
    ("nvidia",              "wd5",  "NVIDIA",              "NVIDIAExternalCareerSite"),
    ("adobe",               "wd5",  "Adobe",               "external_experienced"),
    ("zoom",                "wd5",  "Zoom",                "Zoom"),
    ("workday",             "wd5",  "Workday",             "workday"),
    ("cisco",               "wd5",  "Cisco",               "Cisco_Careers"),
    ("intel",               "wd1",  "Intel",               "External"),
    ("hpe",                 "wd5",  "HPE",                 "Jobsathpe"),
    ("broadcom",            "wd1",  "Broadcom",            "External_Career"),
    # Additional Workday companies
    ("vmware",              "wd1",  "VMware",              "VMware"),
    ("qualcomm",            "wd5",  "Qualcomm",            "QRTGC"),
    ("paypal",              "wd1",  "PayPal",              "jobs"),
    ("ebay",                "wd5",  "eBay",                "Apply"),
    ("paloaltonetworks",    "wd1",  "Palo Alto Networks",  "External"),
    ("snowflake",           "wd5",  "Snowflake",           "Snowflake"),
    ("servicenow",          "wd5",  "ServiceNow",          "External"),
    ("intuit",              "wd5",  "Intuit",              "ERPJobsProfile"),
    ("lyft",                "wd5",  "Lyft",                "lyft"),
    ("uber",                "wd5",  "Uber",                "External_Career_Site"),
    ("twitter",             "wd5",  "X (Twitter)",         "Twitter_External_Career_Site"),
]

# Confirmed career homepage URLs — keys are lowercase company names.
# Used by the dashboard to make company names clickable.
CONFIRMED_CAREER_SITES = {
    # Greenhouse
    "stripe":               "https://stripe.com/jobs",
    "databricks":           "https://www.databricks.com/company/careers",
    "datadog":              "https://www.datadoghq.com/careers/",
    "confluent":            "https://www.confluent.io/careers/",
    "cloudflare":           "https://www.cloudflare.com/careers/",
    "anthropic":            "https://www.anthropic.com/careers",
    "openai":               "https://openai.com/careers",
    "notion":               "https://www.notion.so/careers",
    "figma":                "https://www.figma.com/careers/",
    "airbnb":               "https://careers.airbnb.com/",
    "lyft":                 "https://www.lyft.com/careers",
    "doordash":             "https://careers.doordash.com/",
    "coinbase":             "https://www.coinbase.com/careers",
    "reddit":               "https://www.redditinc.com/careers",
    "robinhood":            "https://careers.robinhood.com/",
    "plaid":                "https://plaid.com/careers/",
    "brex":                 "https://www.brex.com/careers",
    "ramp":                 "https://ramp.com/careers",
    "waymo":                "https://waymo.com/careers/",
    "dropbox":              "https://www.dropbox.com/jobs",
    "asana":                "https://asana.com/jobs",
    "airtable":             "https://airtable.com/careers",
    "instacart":            "https://instacart.careers/",
    "canva":                "https://www.canva.com/careers/",
    "zendesk":              "https://www.zendesk.com/company/careers/",
    "github":               "https://github.com/about/careers",
    "zscaler":              "https://www.zscaler.com/careers",
    "hashicorp":            "https://www.hashicorp.com/jobs",
    "box":                  "https://www.box.com/en-us/careers",
    "twilio":               "https://www.twilio.com/en-us/company/jobs",
    "sendgrid":             "https://www.twilio.com/en-us/company/jobs",
    "pagerduty":            "https://www.pagerduty.com/careers/",
    "new relic":            "https://newrelic.com/about/careers",
    "newrelic":             "https://newrelic.com/about/careers",
    "chime":                "https://careers.chime.com/",
    "affirm":               "https://www.affirm.com/company/careers",
    "carta":                "https://carta.com/careers/",
    "cohere":               "https://cohere.com/careers",
    "mistral ai":           "https://mistral.ai/careers",
    "mistral":              "https://mistral.ai/careers",
    "scale ai":             "https://scale.com/careers",
    "scale":                "https://scale.com/careers",
    "uber":                 "https://www.uber.com/us/en/careers/",
    "unity":                "https://careers.unity.com/",
    "unity technologies":   "https://careers.unity.com/",
    "pinterest":            "https://www.pinterestcareers.com/",
    # Lever
    "netflix":              "https://jobs.netflix.com/",
    "shopify":              "https://www.shopify.com/careers",
    "palantir":             "https://www.palantir.com/careers/",
    "palantir technologies": "https://www.palantir.com/careers/",
    "vercel":               "https://vercel.com/careers",
    "linear":               "https://linear.app/careers",
    "loom":                 "https://www.loom.com/careers",
    "retool":               "https://retool.com/careers",
    "heap":                 "https://heap.io/careers",
    "benchling":            "https://www.benchling.com/careers",
    "amplitude":            "https://amplitude.com/careers",
    "mixpanel":             "https://mixpanel.com/jobs/",
    "grammarly":            "https://www.grammarly.com/jobs",
    "flexport":             "https://www.flexport.com/careers/",
    # Workday
    "salesforce":           "https://salesforce.com/company/careers/",
    "nvidia":               "https://www.nvidia.com/en-us/about-nvidia/careers/",
    "adobe":                "https://www.adobe.com/careers.html",
    "zoom":                 "https://careers.zoom.us/",
    "zoom video communications": "https://careers.zoom.us/",
    "workday":              "https://www.workday.com/en-us/company/careers.html",
    "cisco":                "https://jobs.cisco.com/",
    "cisco systems":        "https://jobs.cisco.com/",
    "intel":                "https://jobs.intel.com/",
    "hpe":                  "https://careers.hpe.com/",
    "hewlett packard enterprise": "https://careers.hpe.com/",
    "broadcom":             "https://careers.broadcom.com/",
    # Ashby
    "rippling":             "https://www.rippling.com/careers",
    "deel":                 "https://www.deel.com/careers",
    "mercury":              "https://mercury.com/jobs",
    "airbyte":              "https://airbyte.com/careers",
    "fivetran":             "https://www.fivetran.com/careers",
    "hightouch":            "https://hightouch.com/careers",
    "dbt labs":             "https://www.getdbt.com/dbt-labs/open-roles/",
    # Greenhouse new
    "elastic":              "https://www.elastic.co/about/careers",
    "mongodb":              "https://www.mongodb.com/careers",
    "couchbase":            "https://www.couchbase.com/careers/",
    "redis":                "https://redis.com/company/careers/",
    "cockroachdb":          "https://www.cockroachlabs.com/careers/",
    "algolia":              "https://www.algolia.com/careers/",
    "contentful":           "https://www.contentful.com/careers/",
    "snyk":                 "https://snyk.io/company/careers/",
    "sentry":               "https://sentry.io/careers/",
    "grafana":              "https://grafana.com/about/careers/",
    "lacework":             "https://www.lacework.com/careers/",
    "discord":              "https://discord.com/careers",
    "twitch":               "https://www.twitch.tv/jobs",
    "duolingo":             "https://careers.duolingo.com/",
    "gitlab":               "https://about.gitlab.com/jobs/",
    "intercom":             "https://www.intercom.com/careers",
    "fastly":               "https://www.fastly.com/about/careers/",
    "miro":                 "https://miro.com/careers/",
    "okta":                 "https://www.okta.com/company/careers/",
    "veeva":                "https://careers.veeva.com/",
    "marqeta":              "https://www.marqeta.com/company/careers/",
    "nerdwallet":           "https://www.nerdwallet.com/careers",
    "blend":                "https://blend.com/company/careers/",
    "lob":                  "https://www.lob.com/careers",
    "samsara":              "https://www.samsara.com/company/careers",
    "coursera":             "https://careers.coursera.com/",
    "faire":                "https://www.faire.com/careers",
    "opendoor":             "https://www.opendoor.com/w/careers",
    "adept":                "https://www.adept.ai/careers",
    # Lever new
    "scaleai":              "https://scale.com/careers",
    "remote":               "https://remote.com/careers",
    "watershed":            "https://watershed.com/careers",
    # Ashby new
    "hex":                  "https://hex.tech/company/careers",
    "dagster":              "https://dagster.io/careers",
    "prefect":              "https://www.prefect.io/careers",
    "modal":                "https://modal.com/careers",
    "together":             "https://www.together.ai/careers",
    "replit":               "https://replit.com/careers",
    "cursor":               "https://www.cursor.com/careers",
    "sourcegraph":          "https://about.sourcegraph.com/jobs",
    "pinecone":             "https://www.pinecone.io/careers/",
    "weaviate":             "https://weaviate.io/company/careers",
    # Workday new
    "vmware":               "https://careers.vmware.com/",
    "qualcomm":             "https://careers.qualcomm.com/",
    "paypal":               "https://careers.pypl.com/",
    "ebay":                 "https://careers.ebay.com/",
    "palo alto networks":   "https://jobs.paloaltonetworks.com/",
    "snowflake":            "https://careers.snowflake.com/",
    # SmartRecruiters
    "servicenow":           "https://careers.servicenow.com/",
    "block":                "https://careers.block.xyz/",
    "square":               "https://careers.block.xyz/",
    # Big tech (LinkedIn-sourced)
    "google":               "https://careers.google.com/",
    "meta":                 "https://www.metacareers.com/",
    "microsoft":            "https://careers.microsoft.com/",
    "amazon":               "https://amazon.jobs/",
    "apple":                "https://jobs.apple.com/",
    "tesla":                "https://www.tesla.com/careers",
    "tiktok":               "https://careers.tiktok.com/",
    "bytedance":            "https://jobs.bytedance.com/",
    "snap":                 "https://careers.snap.com/",
    "snapchat":             "https://careers.snap.com/",
    "twitter":              "https://careers.twitter.com/",
    "x":                    "https://careers.twitter.com/",
    "snowflake":            "https://careers.snowflake.com/",
    "splunk":               "https://www.splunk.com/en_us/careers.html",
    "intuit":               "https://jobs.intuit.com/",
    "okta":                 "https://www.okta.com/company/careers/",
    "qualcomm":             "https://careers.qualcomm.com/",
    "vmware":               "https://careers.vmware.com/",
    "oracle":               "https://careers.oracle.com/",
    "metabase":             "https://www.metabase.com/jobs",
    "descript":             "https://www.descript.com/careers",
}


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
    import asyncio
    levels = levels or []

    db_companies = _load_db_companies()

    async def _safe(coro, label):
        try:
            return await coro
        except Exception as e:
            logger.debug(f"{label}: {e}")
            return []

    # Use a semaphore so we don't hammer every host simultaneously
    sem = asyncio.Semaphore(20)

    async def _guarded(coro, label):
        async with sem:
            return await _safe(coro, label)

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        tasks = []

        for c in db_companies:
            ats = c["ats_type"]
            slug = c["ats_slug"]
            name = c["company_name"]

            if ats == "greenhouse":
                tasks.append(_guarded(_scrape_greenhouse(client, slug, titles, locations, levels), f"Greenhouse/{slug}"))
            elif ats == "lever":
                tasks.append(_guarded(_scrape_lever(client, slug, titles, locations, levels), f"Lever/{slug}"))
            elif ats == "ashby":
                tasks.append(_guarded(_scrape_ashby(client, slug, titles, locations, levels), f"Ashby/{slug}"))
            elif ats == "workday" and c.get("workday_board"):
                tasks.append(_guarded(
                    _scrape_workday(client, slug, c.get("workday_wd_ver", "wd5"), name, c["workday_board"], titles, locations, levels),
                    f"Workday/{slug}",
                ))
            elif ats == "smartrecruiters":
                tasks.append(_guarded(_scrape_smartrecruiters(client, slug, name, titles, locations, levels), f"SmartRecruiters/{slug}"))
            elif ats == "amazon":
                tasks.append(_guarded(_scrape_amazon(client, titles, locations, levels), "Amazon"))
            elif ats == "apple":
                tasks.append(_guarded(_scrape_apple(client, titles, locations, levels), "Apple"))
            elif ats == "workable":
                tasks.append(_guarded(_scrape_workable(client, slug, name, titles, locations, levels), f"Workable/{slug}"))
            elif ats == "rippling":
                tasks.append(_guarded(_scrape_rippling(client, slug, name, titles, locations, levels), f"Rippling/{slug}"))
            elif ats == "custom" and c.get("career_url"):
                from src.scraper.web_search import scrape_via_web_search
                tasks.append(_guarded(
                    scrape_via_web_search(name, c["career_url"], titles, locations, levels),
                    f"WebSearch/{name}",
                ))

        # Job boards — run once per full scrape
        tasks.append(_safe(_scrape_indeed(client, titles, locations, levels), "Indeed"))
        tasks.append(_safe(_scrape_google(client, titles, locations, levels), "Google"))

        # Startup-focused boards
        from src.scraper.boards import _scrape_wellfound, _scrape_yc
        tasks.append(_safe(_scrape_wellfound(client, titles, locations, levels), "Wellfound"))
        tasks.append(_safe(_scrape_yc(client, titles, locations, levels), "YC"))

        all_results = await asyncio.gather(*tasks)

    results = [job for batch in all_results for job in batch]
    logger.info(f"Career pages: found {len(results)} matching jobs")
    return results


async def scrape_linkedin_only(
    titles: list[str],
    locations: list[str],
    levels: list[str] = None,
    geo_id: str = "90000084",
    time_filter: str = "r300",   # last 5 min — matches the 5-min scheduler cadence
) -> list[dict]:
    """Lightweight LinkedIn-only scrape for high-frequency polling."""
    import asyncio
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        return await _scrape_linkedin(client, titles, locations or [], levels or [], geo_id, time_filter)


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
                    "workday_wd_ver": r.workday_wd_ver or "wd5",
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
    try:
        resp = await client.get(url, timeout=10)
    except Exception:
        return []
    if resp.status_code in (401, 403, 404, 422):
        return []
    try:
        data = resp.json()
    except Exception:
        return []

    results = []
    for job in data.get("jobs", []):          # key is "jobs", not "jobPostings"
        if not job.get("isListed", True):
            continue
        loc = job.get("location", "") or ""
        if not _matches_criteria(job.get("title", ""), loc, titles, locations, levels):
            continue
        results.append({
            "company_job_id": job["id"],
            "company_name":   company.replace("-", " ").title(),
            "job_title":      job["title"],
            "location":       loc or None,
            "level":          _infer_level(job["title"]),
            "url":            job.get("jobUrl", f"https://jobs.ashbyhq.com/{company}/{job['id']}"),
            "source":         f"ashby:{company}",
            "description":    job.get("descriptionPlain", "")[:2000] or None,
            "posted_at":      _parse_iso(job.get("publishedAt")),
        })
    return results


async def _scrape_rippling(client, slug, display_name, titles, locations, levels):
    url = f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
    try:
        resp = await client.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    except Exception:
        return []
    if resp.status_code in (401, 403, 404, 422):
        return []
    try:
        data = resp.json()
    except Exception:
        return []

    results = []
    seen = set()
    for job in data if isinstance(data, list) else []:
        job_id = job.get("uuid")
        title = job.get("name", "")
        loc = (job.get("workLocation") or {}).get("label", "") or ""
        if not _matches_criteria(title, loc, titles, locations, levels):
            continue
        key = (job_id, loc)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "company_job_id": job_id,
            "company_name":   display_name,
            "job_title":      title,
            "location":       loc or None,
            "level":          _infer_level(title),
            "url":            job.get("url", f"https://ats.rippling.com/{slug}/jobs/{job_id}"),
            "source":         f"rippling:{slug}",
            "description":    None,
            "posted_at":      None,
        })
    return results


async def _scrape_workday(client, tenant, wd_ver, display_name, board, titles, locations, levels):
    url = f"https://{tenant}.{wd_ver}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
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
            # externalPath is relative to board root (e.g. /job/City/Title_ID)
            # full URL needs the board name prefix
            if path.startswith("/"):
                job_url = f"https://{tenant}.{wd_ver}.myworkdayjobs.com/{board}{path}"
            else:
                job_url = path
            posted_at = _parse_workday_posted(job.get("postedOn"))

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


async def _fetch_linkedin_apply_url(client, job_id: str, headers: dict) -> Optional[str]:
    """
    Fetch LinkedIn job detail page and extract the external apply URL.
    Returns the company's own job posting URL (e.g. tesla.com/careers/...) or None.
    """
    try:
        resp = await client.get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
            headers=headers, timeout=8,
        )
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        # Primary: top-card apply button
        for sel in [
            "a.apply-button--top-card",
            "a[data-tracking-control-name*='apply']",
            ".top-card-layout__cta a[href]",
            "a.top-card-layout__cta",
        ]:
            el = soup.select_one(sel)
            if el and el.get("href", "").startswith("http"):
                href = el["href"].split("?")[0]
                if "linkedin.com" not in href:
                    return href
        # Fallback: JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                data = _json.loads(script.string or "")
                url = data.get("url") or data.get("applyUrl")
                if url and "linkedin.com" not in url:
                    return url.split("?")[0]
            except Exception:
                pass
    except Exception:
        pass
    return None


_ATS_BOARD_TEMPLATES = {
    "greenhouse":      "https://job-boards.greenhouse.io/{slug}",
    "lever":           "https://jobs.lever.co/{slug}",
    "ashby":           "https://jobs.ashbyhq.com/{slug}",
    "workday":         "https://{slug}.myworkdayjobs.com",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
}


def _resolve_ats_url_from_tracked(company_name: str, tracked: dict) -> str | None:
    """
    Given a company name from a LinkedIn result, return the career page URL
    from tracked_companies if we already track this company — no HTTP needed.
    """
    key = company_name.lower().strip()
    # Exact match first
    if key in tracked:
        entry = tracked[key]
        return entry.get("career_url") or _ATS_BOARD_TEMPLATES.get(
            entry["ats_type"], ""
        ).format(slug=entry["ats_slug"])
    # Partial match (e.g. "Adobe" in "Adobe Inc.")
    for tk, entry in tracked.items():
        if tk in key or key in tk:
            return entry.get("career_url") or _ATS_BOARD_TEMPLATES.get(
                entry["ats_type"], ""
            ).format(slug=entry["ats_slug"])
    return None


def _load_tracked_companies_cache() -> dict:
    """Load active tracked companies keyed by lowercase company_name."""
    try:
        from src.api.database import SessionLocal
        from src.api.models import TrackedCompany
        with SessionLocal() as db:
            rows = db.query(TrackedCompany).filter_by(is_active=True).all()
            return {
                r.company_name.lower().strip(): {
                    "ats_type":  r.ats_type,
                    "ats_slug":  r.ats_slug,
                    "career_url": r.career_url,
                }
                for r in rows
            }
    except Exception:
        return {}


async def _scrape_linkedin(
    client,
    titles: list[str],
    locations: list[str],
    levels: list[str],
    geo_id: str = "90000084",   # San Francisco Bay Area
    time_filter: str = "r300",  # last 5 minutes (matches 5-min poll cadence)
) -> list[dict]:
    """
    LinkedIn guest job search — no auth needed.
    Uses geoId for reliable Bay Area targeting and f_TPR=r3600 (last hour)
    so the 5-minute polling cadence captures every new post without flooding.
    Paginate 3 pages per title keyword (25 cards/page = up to 75 listings).
    For each result, fetch the job detail page to capture the company's own apply URL.
    """
    import asyncio as _asyncio

    results = []
    seen = set()
    _USER_AGENTS = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    import random as _random
    detail_fetches = 0  # cap detail page calls per run (for companies not in tracked_companies)
    _tracked_companies_cache = _load_tracked_companies_cache()

    for i, title_kw in enumerate(titles or []):
        # Polite delay between keyword requests to avoid 429
        if i > 0:
            await _asyncio.sleep(3)

        headers = {
            "User-Agent": _random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.linkedin.com/",
        }

        # Only 1 page (25 results) per keyword — with r3600 cadence there won't be more
        for page in range(1):
            params = {
                "keywords": title_kw,
                "geoId": geo_id,
                "start": str(page * 25),
                "f_TPR": time_filter,
                "sortBy": "DD",     # date descending
            }
            try:
                resp = await client.get(
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params=params, headers=headers,
                )
                if resp.status_code == 429:
                    logger.warning(f"LinkedIn 429 on '{title_kw}' — backing off 30s")
                    await _asyncio.sleep(30)
                    break
                if resp.status_code != 200:
                    break
            except Exception:
                break

            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select("li")
            if not cards:
                break   # no more results

            for card in cards:
                link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
                if not link_el:
                    continue
                job_url = link_el.get("href", "").split("?")[0]
                if not job_url or job_url in seen:
                    continue
                seen.add(job_url)

                title_el   = card.select_one(".base-search-card__title, h3")
                company_el = card.select_one(".base-search-card__subtitle, h4")
                loc_el     = card.select_one(".job-search-card__location")
                time_el    = card.select_one("time")

                title   = title_el.get_text(strip=True)  if title_el   else ""
                company = company_el.get_text(strip=True) if company_el else "Unknown"
                loc     = loc_el.get_text(strip=True)    if loc_el     else ""

                if not title or not _matches_criteria(title, loc, titles, locations, levels):
                    continue

                # Skip job aggregators and staffing firms that post noise
                _NOISE_COMPANIES = {
                    "jobs via dice", "dice", "jobright.ai", "jobright", "jack & jill",
                    "eteam", "scale.jobs", "synergisticit", "xcede", "stealth mode",
                    "techclub inc", "techclub", "linkedin news", "jobs via linkedin",
                }
                if company.lower().strip() in _NOISE_COMPANIES:
                    continue

                posted_at = None
                if time_el and time_el.get("datetime"):
                    posted_at = _parse_iso(time_el["datetime"])

                m = re.search(r"/jobs/view/(\d+)", job_url)
                job_id = m.group(1) if m else job_url[-20:]

                # Resolve real ATS URL so cross-source dedup works.
                # 1. Check tracked_companies for a known ATS URL (instant, no HTTP).
                # 2. Fall back to fetching the LinkedIn detail page (capped).
                ats_url = _resolve_ats_url_from_tracked(company, _tracked_companies_cache)
                if not ats_url and detail_fetches < 30:
                    await _asyncio.sleep(0.8)
                    ats_url = await _fetch_linkedin_apply_url(client, job_id, headers)
                    detail_fetches += 1

                # Use ATS URL as primary so dedup works cross-source.
                # Keep LinkedIn URL as original_url for reference.
                if ats_url:
                    primary_url  = ats_url
                    linkedin_ref = job_url
                else:
                    primary_url  = job_url
                    linkedin_ref = None

                results.append({
                    "company_job_id": f"li_{job_id}",
                    "company_name": company,
                    "job_title": title,
                    "location": loc or None,
                    "level": _infer_level(title),
                    "url": primary_url,
                    "original_url": linkedin_ref,
                    "source": "linkedin",
                    "description": None,
                    "posted_at": posted_at,
                })

    return results


async def _scrape_smartrecruiters(client, company_id: str, display_name: str,
                                   titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """SmartRecruiters public postings API — no auth required."""
    results = []
    seen = set()
    for title_kw in (titles or [""]):
        params = {"limit": 100, "offset": 0}
        if title_kw:
            params["q"] = title_kw
        try:
            resp = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings",
                params=params, timeout=12,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception:
            return []

        for job in data.get("content", []):
            job_id = job.get("id", "")
            if job_id in seen:
                continue
            seen.add(job_id)

            title = job.get("name", "")
            loc_obj = job.get("location", {})
            loc_parts = [loc_obj.get("city", ""), loc_obj.get("region", ""), loc_obj.get("country", "")]
            loc = ", ".join(p for p in loc_parts if p)
            if job.get("location", {}).get("remote"):
                loc = "Remote" if not loc else f"{loc} (Remote)"

            if not _matches_criteria(title, loc, titles, locations, levels):
                continue

            ref = job.get("ref", "")
            posted_at = _parse_iso(job.get("releasedDate"))

            results.append({
                "company_job_id": job_id,
                "company_name": display_name,
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": ref,
                "source": f"smartrecruiters:{company_id}",
                "description": None,
                "posted_at": posted_at,
            })
    return results


async def _scrape_workable(client, slug: str, display_name: str,
                           titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """Workable public jobs API — many mid-size companies use this ATS."""
    results = []
    seen: set[str] = set()
    try:
        resp = await client.get(
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            params={"limit": 100},
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    for job in data.get("results", []):
        job_id = job.get("shortcode") or job.get("id") or ""
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        title    = job.get("title") or ""
        loc_city = job.get("city") or ""
        loc_ctry = job.get("country_code") or ""
        remote   = job.get("remote") or False
        loc      = loc_city or loc_ctry or ("Remote" if remote else "")

        if not _matches_criteria(title, loc, titles, locations, levels):
            continue

        job_url  = f"https://apply.workable.com/{slug}/j/{job_id}"
        posted_at = _parse_iso(job.get("published_on") or job.get("created_at"))

        results.append({
            "company_job_id": f"wkbl_{job_id}",
            "company_name":   display_name,
            "job_title":      title,
            "location":       loc or None,
            "level":          _infer_level(title),
            "url":            job_url,
            "source":         f"workable:{slug}",
            "description":    None,
            "posted_at":      posted_at,
        })
    return results


async def _scrape_indeed(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """
    Indeed RSS feed — `fromage=1` = posted today, sorted by date.
    Indeed blocks server requests aggressively; we use the RSS endpoint which
    is lighter and more stable than the HTML search page.
    """
    import xml.etree.ElementTree as ET
    results = []
    seen = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/rss+xml, text/xml, */*",
    }
    loc_param = "San Francisco Bay Area, CA" if locations else ""

    for title_kw in (titles or []):
        params = {
            "q": title_kw,
            "l": loc_param,
            "fromage": "1",    # posted within last 1 day
            "sort": "date",
        }
        try:
            resp = await client.get("https://www.indeed.com/rss", params=params, headers=headers, timeout=10)
            if resp.status_code != 200 or "<rss" not in resp.text:
                continue
            root = ET.fromstring(resp.text)
        except Exception:
            continue

        for item in root.findall(".//item"):
            def tag(name):
                el = item.find(name)
                return el.text.strip() if el is not None and el.text else ""

            title  = tag("title")
            link   = tag("link")
            pub    = tag("pubDate")
            desc   = tag("description")
            # Indeed encodes company/location in the title as "Title - Company - Location"
            company = ""
            loc     = ""
            parts = [p.strip() for p in title.split(" - ")]
            if len(parts) >= 3:
                title   = parts[0]
                company = parts[1]
                loc     = parts[2]
            elif len(parts) == 2:
                title   = parts[0]
                company = parts[1]

            if not link or link in seen:
                continue
            seen.add(link)

            if not _matches_criteria(title, loc, titles, locations, levels):
                continue

            posted_at = None
            try:
                from email.utils import parsedate_to_datetime
                posted_at = parsedate_to_datetime(pub) if pub else None
            except Exception:
                pass

            # Extract job ID from the Indeed URL
            m = re.search(r"jk=([a-f0-9]+)", link)
            job_id = m.group(1) if m else hashlib.md5(link.encode()).hexdigest()[:16]

            results.append({
                "company_job_id": f"indeed_{job_id}",
                "company_name": company or "Unknown",
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": link,
                "source": "indeed",
                "description": BeautifulSoup(desc, "lxml").get_text()[:500] if desc else None,
                "posted_at": posted_at,
            })

    return results


async def _scrape_amazon(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """Amazon Jobs JSON API — publicly accessible, no auth required."""
    results = []
    seen = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json",
    }
    loc_param = "Seattle, WA|San Francisco, CA|Bay Area" if locations else ""

    for title_kw in (titles or [""]):
        try:
            resp = await client.get(
                "https://amazon.jobs/en/search.json",
                params={
                    "base_query": title_kw,
                    "loc_query": "San Francisco Bay Area",
                    "job_count": "25",
                    "offset": "0",
                    "sort": "recent",
                },
                headers=headers,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for job in data.get("jobs", []):
            job_id = str(job.get("id_icims", job.get("id", "")))
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)

            title = job.get("title", "")
            loc = job.get("location", "") or job.get("normalized_location", "")
            if not _matches_criteria(title, loc, titles, locations, levels):
                continue

            path = job.get("job_path", "")
            job_url = f"https://amazon.jobs{path}" if path else f"https://amazon.jobs/en/jobs/{job_id}"
            posted_at = _parse_iso(job.get("posted_date"))

            results.append({
                "company_job_id": f"amz_{job_id}",
                "company_name": "Amazon",
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": job_url,
                "source": "amazon",
                "description": job.get("description_short", "")[:1000] or None,
                "posted_at": posted_at,
            })
    return results


async def _scrape_apple(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """Apple Jobs — search via jobs.apple.com/api/role/search.
    Note: Apple's API blocks datacenter IPs (returns 301→pagenotfound).
    Jobs must be added manually via the Apply flow or by pasting a jobs.apple.com URL.
    """
    results = []
    seen = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://jobs.apple.com",
        "Referer": "https://jobs.apple.com/en-us/search",
    }
    for title_kw in (titles or [""]):
        try:
            resp = await client.post(
                "https://jobs.apple.com/api/role/search",
                json={
                    "query": title_kw,
                    "filters": {},
                    "page": 1,
                    "locale": "en-us",
                    "sort": "relevance",
                },
                headers=headers,
            )
            if resp.status_code != 200:
                logger.debug(f"Apple Jobs API returned {resp.status_code} — likely IP-blocked")
                break
            data = resp.json()
        except Exception as e:
            logger.debug(f"Apple scraper error: {e}")
            break

        for job in data.get("searchResults", []):
            job_id = str(job.get("positionId", ""))
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)

            title = job.get("postingTitle", "") or job.get("title", "")
            loc_parts = job.get("location", {})
            loc = loc_parts.get("name", "") if isinstance(loc_parts, dict) else str(loc_parts)

            if not _matches_criteria(title, loc, titles, locations, levels):
                continue

            posted_at = _parse_iso(job.get("postingDate") or job.get("managedPipelinePostingDate"))
            job_url = f"https://jobs.apple.com/en-us/details/{job_id}"

            results.append({
                "company_job_id": f"apple_{job_id}",
                "company_name": "Apple",
                "job_title": title,
                "location": loc or None,
                "level": _infer_level(title),
                "url": job_url,
                "source": "apple",
                "description": job.get("jobSummary", "")[:1000] or None,
                "posted_at": posted_at,
            })
    return results


async def _scrape_microsoft(client, titles: list[str], locations: list[str], levels: list[str]) -> list[dict]:
    """Microsoft Careers API — public search endpoint."""
    results = []
    seen = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://jobs.careers.microsoft.com",
        "Referer": "https://jobs.careers.microsoft.com/",
    }

    for title_kw in (titles or [""]):
        try:
            resp = await client.get(
                "https://gcsservices.careers.microsoft.com/search/api/v1/search",
                params={
                    "q": title_kw,
                    "l": "San Francisco Bay Area",
                    "pg": "1",
                    "pgSz": "20",
                    "o": "Relevance",
                    "flt": "true",
                },
                headers=headers,
                timeout=12,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        jobs = data.get("operationResult", {}).get("result", {}).get("jobs", [])
        for job in jobs:
            job_id = str(job.get("jobId", ""))
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)

            title = job.get("title", "")
            locs = job.get("properties", {}).get("primaryLocation", "") or ""
            if not _matches_criteria(title, locs, titles, locations, levels):
                continue

            job_url = f"https://jobs.careers.microsoft.com/global/en/job/{job_id}"
            posted_raw = job.get("postingDate", "")
            posted_at = _parse_iso(posted_raw)

            results.append({
                "company_job_id": f"ms_{job_id}",
                "company_name": "Microsoft",
                "job_title": title,
                "location": locs or None,
                "level": _infer_level(title),
                "url": job_url,
                "source": "microsoft",
                "description": job.get("description", "")[:1000] or None,
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

    if locations and location:
        loc_lower = location.lower()
        def _loc_matches(token: str) -> bool:
            t = token.lower()
            # Short tokens (state/country codes) need word-boundary matching
            # to prevent "CA" matching "Casablanca" or "WA" matching "Warsaw"
            if len(t) <= 3:
                return bool(re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', loc_lower))
            return t in loc_lower
        if not any(_loc_matches(loc) for loc in locations):
            return False
    elif locations and not location:
        return False

    if levels:
        user_levels_lower = [l.lower() for l in levels]
        inferred = _infer_level(title)

        if inferred is not None:
            # Inferred level must be in the allowed list
            if inferred.lower() not in user_levels_lower:
                return False
        else:
            # No level keyword in title — if user only wants junior/new-grad,
            # still block titles that have senior-indicator words
            _SENIOR_WORDS = [
                "senior", "sr.", r"\bsr\b", "staff", "principal",
                "distinguished", "lead", "manager", "director",
                "head of", r"\bvp\b", "vice president", "chief",
            ]
            wants_only_junior = all(
                l in ("junior", "new grad", "entry level", "intern", "associate")
                for l in user_levels_lower
            )
            if wants_only_junior:
                t_lower = title.lower()
                if any(re.search(p, t_lower) for p in _SENIOR_WORDS):
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
            m = (re.search(r"/boards/([^/?]+)", path) or
                 re.search(r"^/([^/?]+)", path))
            slug = m.group(1) if m else None
            if slug and slug not in ("v1", "embed"):
                return {"ats_type": "greenhouse", "ats_slug": slug, "workday_board": None}

        if "lever.co" in host:
            m = re.match(r"/([^/?]+)", path)
            slug = m.group(1) if m else None
            if slug:
                return {"ats_type": "lever", "ats_slug": slug, "workday_board": None}

        if "ashbyhq.com" in host:
            m = re.match(r"/([^/?]+)", path)
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


def _parse_workday_posted(s: Optional[str]) -> Optional[datetime]:
    """
    Workday's cxs API returns relative strings, not ISO dates:
    "Posted Today", "Posted Yesterday", "Posted N Days Ago", "Posted 30+ Days Ago".
    "30+" is Workday's own display cap — treated as exactly 30 days old.
    """
    if not s:
        return None
    s = s.strip().lower()
    now = datetime.now(timezone.utc)
    if "today" in s:
        return now
    if "yesterday" in s:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", s)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return None
