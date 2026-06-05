"""
Google Calendar integration — OAuth2 setup + interview event sync.
Uses httpx directly, no google-api-python-client needed.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from src.api.database import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/calendar", tags=["calendar"])

GOOGLE_TOKEN_URL   = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.readonly"


def _get_credentials() -> tuple[str, str]:
    client_id     = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    return client_id, client_secret


def _get_refresh_token() -> str | None:
    with SessionLocal() as db:
        row = db.execute(text(
            "SELECT value FROM kv_store WHERE key = 'google_refresh_token'"
        )).fetchone()
        return row[0] if row else None


def _save_refresh_token(token: str):
    with SessionLocal() as db:
        db.execute(text("""
            INSERT INTO kv_store (key, value) VALUES ('google_refresh_token', :t)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """), {"t": token})
        db.commit()


def get_access_token() -> str | None:
    """Exchange stored refresh token for a short-lived access token."""
    refresh_token = _get_refresh_token()
    if not refresh_token:
        return None
    client_id, client_secret = _get_credentials()
    if not client_id or not client_secret:
        return None
    try:
        resp = httpx.post(GOOGLE_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }, timeout=10)
        if resp.is_success:
            return resp.json().get("access_token")
        logger.warning(f"Google token refresh failed: {resp.text}")
    except Exception as e:
        logger.warning(f"Google token refresh error: {e}")
    return None


@router.get("/auth-url")
def get_auth_url():
    """Return the Google OAuth2 consent URL for the user to visit."""
    client_id, _ = _get_credentials()
    if not client_id:
        raise HTTPException(400, "GOOGLE_CLIENT_ID not configured in .env")
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri=urn:ietf:wg:oauth:2.0:oob"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return {"url": url}


class CodeExchange(BaseModel):
    code: str

@router.post("/exchange")
def exchange_code(body: CodeExchange):
    """Exchange an authorization code for a refresh token and store it."""
    client_id, client_secret = _get_credentials()
    if not client_id or not client_secret:
        raise HTTPException(400, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")
    try:
        resp = httpx.post(GOOGLE_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "code":          body.code.strip(),
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
        }, timeout=10)
        data = resp.json()
        if not resp.is_success or "refresh_token" not in data:
            raise HTTPException(400, f"Token exchange failed: {data.get('error_description', resp.text)}")
        _save_refresh_token(data["refresh_token"])
        return {"ok": True, "message": "Google Calendar connected successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/status")
def get_status():
    client_id, _ = _get_credentials()
    has_token = _get_refresh_token() is not None
    return {
        "client_id_set":    bool(client_id),
        "refresh_token_set": has_token,
        "ready":            bool(client_id) and has_token,
    }


@router.post("/sync")
def sync_now():
    """Manually trigger a calendar → interviews sync."""
    result = sync_calendar_interviews()
    return result


def sync_calendar_interviews() -> dict:
    """
    Fetch upcoming Google Calendar events, find interview-related ones,
    match to applications by company name, and create Interview records.
    """
    from src.api.models import Application, Interview, Job
    import re

    access_token = get_access_token()
    if not access_token:
        return {"error": "Google Calendar not connected", "created": 0}

    # Fetch events: past 30 days + next 60 days so we catch recent/ongoing interviews
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=30)
    end   = now + timedelta(days=60)
    params = {
        "timeMin":      start.isoformat(),
        "timeMax":      end.isoformat(),
        "singleEvents": "true",
        "orderBy":      "startTime",
        "maxResults":   250,
    }
    try:
        resp = httpx.get(
            f"{GOOGLE_CALENDAR_URL}/calendars/hcliu.jimmy@gmail.com/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=15,
        )
        if not resp.is_success:
            return {"error": f"Calendar API error: {resp.status_code}", "created": 0}
        events = resp.json().get("items", [])
    except Exception as e:
        return {"error": str(e), "created": 0}

    _INTERVIEW_KEYWORDS = re.compile(
        r"\b(interview|phone\s+screen|screening|on.?site|technical|hiring|recruiter"
        r"|zoom\s+call|video\s+call|virtual|meet(?:ing)?|chat|call)\b",
        re.IGNORECASE,
    )

    db = SessionLocal()
    try:
        # Build lookup: normalized company name → application_id
        apps = (
            db.query(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .filter(Application.status.in_(["applied", "phone_screen", "interview"]))
            .all()
        )
        company_map: dict[str, int] = {}       # normalized name → app_id
        domain_map:  dict[str, int] = {}       # domain keyword → app_id
        for app, job in apps:
            norm = re.sub(r"[^a-z0-9]", "", job.company_name.lower())
            company_map[norm] = app.id
            # Build domain keyword: "Samara Living Inc" → "samara"
            first_word = re.sub(r"[^a-z0-9]", "", job.company_name.lower().split()[0])
            if len(first_word) >= 4:
                domain_map[first_word] = app.id

        created = 0
        matched = []
        for event in events:
            title       = event.get("summary", "")
            description = re.sub(r"<[^>]+>", " ", event.get("description", "") or "")
            attendees   = [a.get("email", "") for a in event.get("attendees", [])]
            organizer   = event.get("organizer", {}).get("email", "")
            text_blob   = title + " " + description

            if not _INTERVIEW_KEYWORDS.search(text_blob):
                continue

            # Parse start datetime
            start = event.get("start", {})
            dt_str = start.get("dateTime") or start.get("date")
            if not dt_str:
                continue
            try:
                if "T" in dt_str:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                else:
                    dt = datetime.strptime(dt_str, "%Y-%m-%d")
            except Exception:
                continue

            # Platforms that appear in calendar events but aren't companies
            _PLATFORM_DOMAINS = {
                "google", "gmail", "zoom", "linkedin", "calendly", "microsoft",
                "teams", "webex", "whereby", "meet", "notion", "slack", "greenhouse",
                "lever", "ashby", "workday", "smartrecruiters",
            }

            # Match 1: company name in event title only (descriptions are full of
            # Google Meet links, LinkedIn URLs, etc. that cause false positives)
            app_id = None
            matched_company = None
            norm_title = re.sub(r"[^a-z0-9]", "", title.lower())
            for norm_name, aid in company_map.items():
                if norm_name and len(norm_name) >= 4 and norm_name not in _PLATFORM_DOMAINS and norm_name in norm_title:
                    app_id = aid
                    matched_company = norm_name
                    break

            # Match 2: non-platform attendee/organizer email domain → company first word
            if not app_id:
                all_emails = attendees + [organizer]
                for email_addr in all_emails:
                    if not email_addr or "@" not in email_addr:
                        continue
                    domain = email_addr.split("@")[-1].lower()
                    domain_norm = re.sub(r"[^a-z0-9]", "", domain)
                    # Skip platform/calendar service domains
                    if any(plat in domain_norm for plat in _PLATFORM_DOMAINS):
                        continue
                    for keyword, aid in domain_map.items():
                        if keyword and keyword in domain_norm:
                            app_id = aid
                            matched_company = f"{keyword} (via email domain)"
                            break
                    if app_id:
                        break

            if not app_id:
                logger.debug(f"Calendar event not matched to any app: {title!r}")
                continue

            # Skip if interview already exists on this day for this app
            existing = db.execute(text("""
                SELECT id FROM interviews
                WHERE application_id = :a AND DATE(scheduled_at) = DATE(:d)
            """), {"a": app_id, "d": dt}).fetchone()
            if existing:
                continue

            # Infer round from title
            from src.email.reader import _infer_round
            round_name = _infer_round(title, description)

            db.execute(text("""
                INSERT INTO interviews (application_id, round, scheduled_at, notes)
                VALUES (:a, :r, :d, :n)
            """), {
                "a": app_id, "r": round_name, "d": dt,
                "n": f"From Google Calendar: {title[:200]}",
            })
            created += 1
            matched.append({"title": title, "company": matched_company, "date": dt_str, "round": round_name})
            logger.info(f"Calendar sync: created interview for app {app_id} — {title!r} on {dt.date()}")

        db.commit()
        return {"created": created, "events_scanned": len(events), "matched": matched}

    except Exception as e:
        logger.error(f"Calendar sync error: {e}", exc_info=True)
        return {"error": str(e), "created": 0}
    finally:
        db.close()
