"""
System health diagnostics for the periodic health-check agent.

GET  /system-health/diagnostics  — raw, deterministic checks of every subsystem
                                    (scraper, email sync, proxies, ATS boards…)
POST /system-health/report       — agent submits its reviewed assessment
GET  /system-health/reports      — dashboard reads report history
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from src.api.database import SessionLocal
from src.api.models import SystemHealthReport

router = APIRouter(prefix="/system-health", tags=["system-health"])


# ── Individual checks ───────────────────────────────────────────────────────

def _check_scraper(db) -> dict:
    from src.scraper.engine import get_status
    status = get_status()
    last_run = status.get("last_run")
    age_min = None
    if last_run:
        age_min = (datetime.utcnow() - last_run).total_seconds() / 60
    if not last_run or (age_min is not None and age_min > 90):
        level = "critical"
    elif status.get("errors_last_run", 0) > 0:
        level = "warn"
    else:
        level = "ok"
    return {
        "name": "career_page_scraper",
        "level": level,
        "last_run": last_run.isoformat() if last_run else None,
        "minutes_since_last_run": round(age_min, 1) if age_min is not None else None,
        "jobs_found_last_run": status.get("jobs_found_last_run"),
        "errors_last_run": status.get("errors_last_run"),
        "total_runs": status.get("total_runs"),
        "is_running": status.get("is_running"),
    }


def _check_linkedin(db) -> dict:
    row = db.execute(text(
        "SELECT MAX(discovered_at) FROM jobs WHERE source = 'linkedin' OR source LIKE 'linkedin:%'"
    )).fetchone()
    last = row[0] if row else None
    age_min = None
    if last:
        last_dt = datetime.fromisoformat(last)
        age_min = (datetime.utcnow() - last_dt).total_seconds() / 60
    # Polls every 5 min — anything found in the last 24h with the poll alive is fine;
    # this only signals trouble if NOTHING has ever shown up in a long time.
    level = "warn" if (age_min is None or age_min > 1440) else "ok"
    return {
        "name": "linkedin_scraper",
        "level": level,
        "last_job_discovered_at": last,
        "minutes_since_last_job": round(age_min, 1) if age_min is not None else None,
    }


def _check_email_sync(db) -> dict:
    row = db.execute(text("SELECT MAX(processed_at) FROM email_events")).fetchone()
    last = row[0] if row else None
    age_min = None
    if last:
        last_dt = datetime.fromisoformat(last)
        age_min = (datetime.utcnow() - last_dt).total_seconds() / 60

    # Live IMAP login test — direct ground truth on credentials/connectivity,
    # not just "did the cron fire."
    imap_ok, imap_error = _test_imap_login()

    if not imap_ok:
        level = "critical"
    elif age_min is None or age_min > 60:
        level = "warn"
    else:
        level = "ok"

    return {
        "name": "email_sync",
        "level": level,
        "last_event_processed_at": last,
        "minutes_since_last_event": round(age_min, 1) if age_min is not None else None,
        "imap_login_ok": imap_ok,
        "imap_error": imap_error,
    }


def _test_imap_login() -> tuple[bool, str | None]:
    import imaplib
    email_addr = os.getenv("EMAIL_ADDRESS", "")
    password = os.getenv("EMAIL_APP_PASSWORD", "").replace(" ", "")
    host = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
    port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
    if not email_addr or not password:
        return False, "EMAIL_ADDRESS or EMAIL_APP_PASSWORD not configured"
    try:
        mail = imaplib.IMAP4_SSL(host, port)
        mail.login(email_addr, password)
        mail.select("INBOX", readonly=True)
        mail.logout()
        return True, None
    except Exception as e:
        return False, str(e)[:200]


def _check_brave(db) -> dict:
    # Brave is no longer called by the scraper engine (src/scraper/engine.py) — across the
    # entire job history it ever surfaced 1 job while burning 2x its monthly quota, so the
    # broad-web-search layer was removed in favor of direct ATS scrapers + DDG. This check
    # only reports leftover historical usage; it should never block or warn on its own.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month = today[:7]
    row = db.execute(text(
        "SELECT SUM(calls) FROM api_usage WHERE service='brave' AND date LIKE :m"
    ), {"m": f"{month}%"}).fetchone()
    used = row[0] or 0
    return {
        "name": "brave_search_api",
        "level": "ok",
        "disabled": True,
        "monthly_used_historical": used,
    }


def _check_webshare() -> dict:
    proxy = os.getenv("WEB_SEARCH_PROXY", "")
    if not proxy:
        return {"name": "webshare_proxy", "level": "warn", "configured": False,
                "message": "WEB_SEARCH_PROXY not set — DDG fallback has no proxy"}
    try:
        with httpx.Client(proxy=proxy, timeout=12) as client:
            resp = client.get("https://httpbin.org/ip")
            ok = resp.status_code == 200
            return {
                "name": "webshare_proxy", "level": "ok" if ok else "critical",
                "configured": True, "live_test_status": resp.status_code,
            }
    except Exception as e:
        return {
            "name": "webshare_proxy", "level": "critical", "configured": True,
            "live_test_error": str(e)[:200],
        }


def _check_ddg_direct() -> dict:
    """Direct (no-proxy) reachability of the search engines web_search.py depends on."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text("site:greenhouse.io software engineer", max_results=3))
        ok = len(results) > 0
        return {"name": "ddg_direct_search", "level": "ok" if ok else "warn",
                "results_returned": len(results)}
    except Exception as e:
        return {"name": "ddg_direct_search", "level": "warn", "error": str(e)[:200]}


async def _check_workday_boards(db) -> dict:
    rows = db.execute(text(
        "SELECT company_name, ats_slug, workday_board, workday_wd_ver "
        "FROM tracked_companies WHERE ats_type='workday' AND is_active=1"
    )).fetchall()
    total = len(rows)
    no_board = [r[0] for r in rows if not r[2]]
    to_check = [r for r in rows if r[2]]

    sem = asyncio.Semaphore(15)
    failing = []

    async def ping(name, tenant, board, wd_ver):
        url = f"https://{tenant}.{wd_ver}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json={"limit": 1, "offset": 0, "searchText": ""},
                                              headers={"Content-Type": "application/json"})
                    if resp.status_code != 200:
                        failing.append({"company": name, "status": resp.status_code})
            except Exception as e:
                failing.append({"company": name, "status": "error", "detail": str(e)[:80]})

    await asyncio.gather(*(ping(name, slug, board, wd_ver) for name, slug, board, wd_ver in to_check))

    ok_count = total - len(no_board) - len(failing)
    if len(failing) > 5:
        level = "warn"
    elif len(no_board) > 0 or failing:
        level = "warn" if (len(no_board) + len(failing)) / max(total, 1) > 0.1 else "ok"
    else:
        level = "ok"
    return {
        "name": "workday_boards",
        "level": level,
        "total_companies": total,
        "ok": ok_count,
        "missing_board": no_board,
        "failing": failing,
    }


def _check_db_pipeline(db) -> dict:
    row = db.execute(text("SELECT MAX(discovered_at) FROM jobs")).fetchone()
    last = row[0] if row else None
    age_min = None
    if last:
        age_min = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() / 60
    level = "critical" if (age_min is None or age_min > 180) else "ok"
    active_jobs = db.execute(text("SELECT COUNT(*) FROM jobs WHERE is_active=1")).fetchone()[0]
    active_apps = db.execute(text("SELECT COUNT(*) FROM applications")).fetchone()[0]
    return {
        "name": "job_discovery_pipeline",
        "level": level,
        "last_job_discovered_at": last,
        "minutes_since_last_job": round(age_min, 1) if age_min is not None else None,
        "active_jobs": active_jobs,
        "total_applications": active_apps,
    }


def _check_scheduler() -> dict:
    try:
        from src.api.scheduler import scheduler
        jobs = scheduler.get_jobs()
        running = scheduler.running
        job_info = [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
                    for j in jobs]
        expected_ids = {"scraper_10min", "linkedin_poll", "email_sync", "company_discovery", "job_validator"}
        present_ids = {j.id for j in jobs}
        missing = sorted(expected_ids - present_ids)
        level = "critical" if (not running or missing) else "ok"
        return {"name": "scheduler", "level": level, "running": running,
                "jobs": job_info, "missing_jobs": missing}
    except Exception as e:
        return {"name": "scheduler", "level": "critical", "error": str(e)[:200]}


# ── Aggregate endpoint ───────────────────────────────────────────────────────

@router.get("/diagnostics")
async def get_diagnostics():
    with SessionLocal() as db:
        components = [
            _check_scraper(db),
            _check_linkedin(db),
            _check_email_sync(db),
            _check_brave(db),
            _check_webshare(),
            _check_ddg_direct(),
            await _check_workday_boards(db),
            _check_db_pipeline(db),
            _check_scheduler(),
        ]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }


# ── Agent report submission + history ───────────────────────────────────────

class ComponentReport(BaseModel):
    name: str
    status: str   # ok | warn | critical
    message: str


class HealthReportIn(BaseModel):
    overall_status: str   # ok | warn | critical
    summary: str
    components: list[ComponentReport]
    diagnostics: dict | None = None


@router.post("/report", status_code=201)
def submit_report(body: HealthReportIn):
    with SessionLocal() as db:
        report = SystemHealthReport(
            overall_status=body.overall_status,
            summary=body.summary,
            components_json=json.dumps([c.model_dump() for c in body.components]),
            diagnostics_json=json.dumps(body.diagnostics) if body.diagnostics else None,
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        return {"id": report.id, "created_at": report.created_at.isoformat()}


@router.get("/reports")
def list_reports(limit: int = 20):
    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT id, created_at, overall_status, summary, components_json, diagnostics_json
            FROM system_health_reports ORDER BY created_at DESC LIMIT :limit
        """), {"limit": limit}).fetchall()
    return {
        "reports": [
            {
                "id": r[0], "created_at": r[1], "overall_status": r[2], "summary": r[3],
                "components": json.loads(r[4]) if r[4] else [],
                "diagnostics": json.loads(r[5]) if r[5] else None,
            }
            for r in rows
        ]
    }
