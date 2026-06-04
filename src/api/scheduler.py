import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

PT = "America/Los_Angeles"   # handles PST/PDT automatically
scheduler = AsyncIOScheduler(timezone=PT)


def start_scheduler():
    from src.scraper.engine import run_scraper, run_linkedin_scraper
    from src.discord.notifications import send_morning_summary, send_evening_checkin, send_daily_report
    from src.api.learning import run_learning_pass
    from src.email.reader import run_email_sync
    from src.scraper.web_search import run_company_discovery
    from src.scraper.validator import run_validation

    # Career page scrapers every 30 minutes (concurrent scrape takes ~1-2 min now)
    scheduler.add_job(
        run_scraper,
        trigger=IntervalTrigger(minutes=30),
        id="scraper_10min",
        replace_existing=True,
        max_instances=1,
    )

    # LinkedIn-only poll every 5 minutes: f_TPR=r300 (last 5 min) + geoId=90000084 (SF Bay Area)
    scheduler.add_job(
        run_linkedin_scraper,
        trigger=IntervalTrigger(minutes=5),
        id="linkedin_poll",
        replace_existing=True,
        max_instances=1,
    )

    # Feedback learning pass every hour
    scheduler.add_job(
        run_learning_pass,
        trigger=IntervalTrigger(hours=1),
        id="learning_pass",
        replace_existing=True,
        max_instances=1,
    )

    # Email inbox sync every 15 minutes
    scheduler.add_job(
        run_email_sync,
        trigger=IntervalTrigger(minutes=15),
        id="email_sync",
        replace_existing=True,
        max_instances=1,
    )

    # Company auto-discovery via web search, daily at 2 AM PT
    scheduler.add_job(
        run_company_discovery,
        trigger=CronTrigger(hour=2, minute=0, timezone=PT),
        id="company_discovery",
        replace_existing=True,
    )

    # Job link validator: every 4 hours at staggered PT times
    scheduler.add_job(
        run_validation,
        trigger=CronTrigger(hour="1,5,9,13,17,21", minute=0, timezone=PT),
        id="job_validator",
        replace_existing=True,
        max_instances=1,
    )

    # Morning summary at 9:00 AM PT
    scheduler.add_job(
        send_morning_summary,
        trigger=CronTrigger(hour=9, minute=0, timezone=PT),
        id="morning_summary",
        replace_existing=True,
    )

    # Evening check-in at 6:00 PM PT
    scheduler.add_job(
        send_evening_checkin,
        trigger=CronTrigger(hour=18, minute=0, timezone=PT),
        id="evening_checkin",
        replace_existing=True,
    )

    # Daily report at 9:00 PM PT
    scheduler.add_job(
        send_daily_report,
        trigger=CronTrigger(hour=21, minute=0, timezone=PT),
        id="daily_report",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown(wait=False)
