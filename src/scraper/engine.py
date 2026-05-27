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
from src.api.models import Job, SearchConfig
from src.scraper import brave, career_pages

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
        new_count, dup_count = _save_jobs(all_jobs)
        stats.new_jobs = new_count
        stats.duplicates_skipped = dup_count

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


def _load_config() -> SearchConfig | None:
    with SessionLocal() as db:
        return db.query(SearchConfig).filter_by(is_active=True).first()


def _save_jobs(raw_jobs: list[dict]) -> tuple[int, int]:
    new_count = 0
    dup_count = 0

    with SessionLocal() as db:
        for data in raw_jobs:
            try:
                job = Job(
                    company_job_id=data["company_job_id"],
                    company_name=data["company_name"],
                    job_title=data["job_title"],
                    location=data.get("location"),
                    level=data.get("level"),
                    url=data["url"],
                    source=data["source"],
                    description=data.get("description"),
                    posted_at=data.get("posted_at"),
                )
                db.add(job)
                db.flush()  # detect constraint violation before commit
                new_count += 1
            except IntegrityError:
                db.rollback()
                dup_count += 1
            except Exception as e:
                db.rollback()
                logger.warning(f"Failed to save job: {e}")

        if new_count > 0:
            db.commit()

    return new_count, dup_count
