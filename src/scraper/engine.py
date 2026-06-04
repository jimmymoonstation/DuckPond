"""
Scraper engine: orchestrates Brave Search + career page scrapers,
deduplicates results, and persists new jobs to the database.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.database import SessionLocal
from src.api.models import Job, SearchConfig, TrackedCompany
from src.scraper import brave, career_pages
from src.scraper.career_pages import scrape_linkedin_only

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime = None
    sources_checked: int = 0
    new_jobs: int = 0
    duplicates_skipped: int = 0
    errors: int = 0

    def finish(self):
        self.finished_at = datetime.utcnow()

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0


# In-memory run history (last 50 runs)
_run_history: list[RunStats] = []
_is_running = False


def get_status() -> dict:
    last = _run_history[-1] if _run_history else None
    return {
        "last_run": last.finished_at if last else None,
        "jobs_found_last_run": last.new_jobs if last else 0,
        "total_runs": len(_run_history),
        "errors_last_run": last.errors if last else 0,
        "is_running": _is_running,
    }


async def run_scraper() -> RunStats:
    global _is_running
    if _is_running:
        logger.info("Scraper already running, skipping")
        return RunStats()

    _is_running = True
    stats = RunStats()

    try:
        from src.scraper.web_search import reset_ddg_circuit
        from src.scraper.brave import reset_brave_circuit
        reset_ddg_circuit()
        reset_brave_circuit()

        config = _load_config()
        titles = json.loads(config.titles_json) if config else []
        locations = json.loads(config.locations_json) if config else []
        keywords = json.loads(config.keywords_json) if config else []
        levels = json.loads(config.levels_json) if config else []

        if not titles:
            logger.info("No search titles configured — skipping scraper run")
            stats.finish()
            return stats

        # Layer 1: Brave Search (broad web)
        brave_jobs = await brave.search_jobs(titles, locations, keywords)
        stats.sources_checked += 1

        # Layer 2: Career pages (targeted)
        career_jobs = await career_pages.scrape_all(titles, locations, levels)
        stats.sources_checked += len(career_pages.GREENHOUSE_COMPANIES) + len(career_pages.LEVER_COMPANIES)

        all_jobs = brave_jobs + career_jobs
        logger.info(f"Raw results: {len(brave_jobs)} brave + {len(career_jobs)} career pages")

        # Persist
        new_jobs_list, new_count, dup_count = _save_jobs_with_list(all_jobs)
        stats.new_jobs = new_count
        stats.duplicates_skipped = dup_count

        # Notify Discord about new finds
        if new_jobs_list:
            from src.discord.notifications import notify_new_jobs
            import asyncio
            asyncio.ensure_future(notify_new_jobs(new_jobs_list))

    except Exception as e:
        logger.error(f"Scraper run failed: {e}", exc_info=True)
        stats.errors += 1
    finally:
        stats.finish()
        _is_running = False
        _run_history.append(stats)
        if len(_run_history) > 50:
            _run_history.pop(0)

    logger.info(
        f"Scraper done in {stats.duration_seconds:.1f}s — "
        f"{stats.new_jobs} new, {stats.duplicates_skipped} dupes"
    )
    return stats


_linkedin_running = False


async def run_linkedin_scraper() -> RunStats:
    """
    LinkedIn-only scrape run for high-frequency (every 5 min) polling.
    Uses f_TPR=r300 (last 5 minutes) + geoId=90000084 (SF Bay Area) —
    matches the 5-min poll interval for minute-level freshness.
    """
    global _linkedin_running
    if _linkedin_running:
        logger.info("LinkedIn scraper already running, skipping")
        return RunStats()

    _linkedin_running = True
    stats = RunStats()

    try:
        config = _load_config()
        titles = json.loads(config.titles_json) if config else []
        locations = json.loads(config.locations_json) if config else []
        levels = json.loads(config.levels_json) if config else []

        if not titles:
            stats.finish()
            return stats

        jobs = await scrape_linkedin_only(titles, locations, levels)
        logger.info(f"LinkedIn poll: {len(jobs)} raw results")

        new_jobs_list, new_count, dup_count = _save_jobs_with_list(jobs)
        stats.new_jobs = new_count
        stats.duplicates_skipped = dup_count

        if new_jobs_list:
            from src.discord.notifications import notify_new_jobs
            import asyncio
            asyncio.ensure_future(notify_new_jobs(new_jobs_list))

    except Exception as e:
        logger.error(f"LinkedIn scraper run failed: {e}", exc_info=True)
        stats.errors += 1
    finally:
        stats.finish()
        _linkedin_running = False

    if stats.new_jobs:
        logger.info(f"LinkedIn: {stats.new_jobs} new jobs saved")

    return stats


def _load_config() -> SearchConfig | None:
    with SessionLocal() as db:
        return db.query(SearchConfig).filter_by(is_active=True).first()


_SOURCE_PRIORITY = {
    'greenhouse': 1, 'lever': 2, 'ashby': 3,
    'workday': 4, 'smartrecruiters': 5, 'amazon': 6,
    'brave_search': 7, 'linkedin': 8, 'extension': 9, 'manual': 10,
}


def _source_rank(source: str) -> int:
    base = source.split(':')[0]
    return _SOURCE_PRIORITY.get(base, 99)


import re as _re_engine

def _co_key(s: str) -> str:
    """Normalize company name for dedup: strip spaces/punctuation/case."""
    return _re_engine.sub(r'[\s\-_.,&\'"]+', '', s.lower()).strip()


def _ttl_key(s: str) -> str:
    return _re_engine.sub(r'[^a-z0-9]', '', s.lower())


def _is_duplicate(db, company_name: str, job_title: str, url: str,
                  original_url: str | None, source: str) -> bool:
    """
    Return True if this job should be skipped as a duplicate.
    Checks (in order):
      1. Exact URL match — same URL already in DB regardless of source
      2. Same company+title already exists from an equal or better source
      3. Same company+title already has an application (never show it again)
    """
    from sqlalchemy import func
    from src.api.models import Application

    # 1. URL dedup (cross-source)
    url_checks = [url]
    if original_url:
        url_checks.append(original_url)
    for u in url_checks:
        if db.query(Job).filter(
            (Job.url == u) | (Job.original_url == u)
        ).first():
            return True

    co_key        = _co_key(company_name)
    ttl_key       = _ttl_key(job_title)
    incoming_rank = _source_rank(source)

    # 2. Same company+title already exists.
    # If existing source is better or equal → skip incoming.
    # If existing source is worse → deactivate it so the better-source version can replace it.
    candidates = db.query(Job).filter(
        func.lower(func.trim(Job.job_title)) == job_title.strip().lower(),
        Job.is_active == True,
    ).all()
    for existing in candidates:
        if _co_key(existing.company_name) == co_key:
            existing_rank = _source_rank(existing.source)
            if existing_rank <= incoming_rank:
                # Existing is equal or better source — skip the incoming
                return True
            else:
                # Incoming is a better source — deactivate the weaker duplicate
                existing.is_active = False

    # 3. Already applied to this company+title
    applied_jobs = (
        db.query(Job)
        .join(Application, Application.job_id == Job.id)
        .filter(func.lower(func.trim(Job.job_title)) == job_title.strip().lower())
        .all()
    )
    for j in applied_jobs:
        if _co_key(j.company_name) == co_key:
            return True

    return False


def _auto_add_company_background(company_name: str, job_url: str, source: str) -> None:
    """
    Fire-and-forget: if the company isn't tracked yet, try to detect their ATS
    and add them so future scrapes cover them.
    Runs in a thread to avoid blocking the save loop.
    """
    import re as _re
    import threading

    def _run():
        import asyncio
        try:
            # Try to derive ATS from the job URL directly (fastest path)
            ats_type, ats_slug = None, None
            patterns = [
                (r"boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([^/?\s]+)", "greenhouse"),
                (r"jobs\.lever\.co/([^/?\s]+)", "lever"),
                (r"jobs\.ashbyhq\.com/([^/?\s]+)", "ashby"),
                (r"([\w-]+)\.(?:wd5|wd1|wd12)\.myworkdayjobs\.com", "workday"),
                (r"jobs\.smartrecruiters\.com/([^/?\s]+)", "smartrecruiters"),
            ]
            for pat, ats in patterns:
                m = _re.search(pat, job_url, _re.IGNORECASE)
                if m:
                    ats_type, ats_slug = ats, m.group(1).lower().rstrip("/")
                    break

            with SessionLocal() as db:
                # Double-check not already added by another thread
                from src.api.models import TrackedCompany as TC
                if db.query(TC).filter(
                    TC.company_name.ilike(company_name), TC.is_active == True
                ).first():
                    return

                probe = None
                if ats_type and ats_slug:
                    probe = {"ats_type": ats_type, "ats_slug": ats_slug, "job_count": 1}
                else:
                    # Fall back to probing common ATS slugs
                    from src.api.routes.companies import _auto_probe
                    probe = asyncio.run(_auto_probe(company_name))

                if not probe:
                    return

                from src.api.routes.companies import _save_company
                existing = db.query(TC).filter_by(
                    ats_type=probe["ats_type"], ats_slug=probe["ats_slug"]
                ).first()
                if not existing:
                    _save_company(db, company_name, probe)
                    logger.info(f"Auto-added company: {company_name} ({probe['ats_type']}:{probe['ats_slug']})")
        except Exception as e:
            logger.debug(f"Auto-add company failed for {company_name}: {e}")

    threading.Thread(target=_run, daemon=True).start()


def _save_jobs(raw_jobs: list[dict]) -> tuple[int, int]:
    _, new_count, dup_count = _save_jobs_with_list(raw_jobs)
    return new_count, dup_count


def _save_jobs_with_list(raw_jobs: list[dict]) -> tuple[list[dict], int, int]:
    new_count = 0
    dup_count = 0
    saved: list[dict] = []

    with SessionLocal() as db:
        # Build normalized name → tracked display name map once per batch
        _tracked_name_map = {
            _co_key(r.company_name): r.company_name
            for r in db.query(TrackedCompany).filter_by(is_active=True).all()
        }

        for data in raw_jobs:
            # Canonicalize company name to tracked_companies display name
            raw_co = data["company_name"]
            data["company_name"] = _tracked_name_map.get(_co_key(raw_co), raw_co)

            # Skip duplicate jobs
            if _is_duplicate(
                db,
                data["company_name"],
                data["job_title"],
                data["url"],
                data.get("original_url"),
                data.get("source", ""),
            ):
                dup_count += 1
                continue

            try:
                job = Job(
                    company_job_id=data["company_job_id"],
                    company_name=data["company_name"],
                    job_title=data["job_title"],
                    location=data.get("location"),
                    level=data.get("level"),
                    url=data["url"],
                    original_url=data.get("original_url"),
                    source=data["source"],
                    description=data.get("description"),
                    posted_at=data.get("posted_at"),
                    tags=data.get("tags"),
                )
                # Use a nested savepoint so a constraint violation on one row
                # doesn't invalidate the entire session / previously-added rows.
                with db.begin_nested():
                    db.add(job)
                new_count += 1
                saved.append(data)

                # Auto-add to tracked_companies if not already present
                co_name = data["company_name"]
                if _co_key(co_name) not in _tracked_name_map:
                    _auto_add_company_background(co_name, data.get("url", ""), data.get("source", ""))

            except IntegrityError:
                dup_count += 1
            except Exception as e:
                logger.warning(f"Failed to save job: {e}")

        if new_count > 0:
            db.commit()

    return saved, new_count, dup_count
