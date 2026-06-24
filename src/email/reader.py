"""
IMAP reader — fetches new job-related emails from Gmail and saves them
as email_events, updating application statuses when matched.
"""
import email
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

logger = logging.getLogger(__name__)

# Priority order for app.status field values (higher = further in pipeline)
_STATUS_PRIORITY = {
    "saved":        0,
    "applied":      1,
    "phone_screen": 2,
    "interview":    3,
    "offer":        4,
    "rejected":     5,
    "withdrawn":    5,
}

_CATEGORY_TO_STATUS = {
    "application_confirm": "applied",
    "interview":           "interview",
    "offer":               "offer",
    # rejection: only auto-update when company match is high-confidence (not body-only)
    "rejection":           "rejected",
    # recruiter: no status change — just informational
}

# Categories that should NOT auto-update application status from a body-only company match
_REQUIRES_CONFIDENT_MATCH = {"rejection"}


def _decode_str(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    parts = decode_header(value)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def _get_body(msg) -> str:
    """Extract plain-text body from email message, falling back to stripped HTML."""
    body = ""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            if ct == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    pass
                if len(body) > 3000:
                    break
            elif ct == "text/html" and not html_fallback:
                try:
                    raw = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    html_fallback = re.sub(r"<[^>]+>", " ", raw)
                    html_fallback = re.sub(r"\s{2,}", " ", html_fallback).strip()
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            pass
    return (body or html_fallback)[:3000]


def _extract_ical_date(msg) -> datetime | None:
    """Look for a text/calendar MIME part and parse DTSTART from the VEVENT block."""
    for part in msg.walk():
        if part.get_content_type() not in ("text/calendar", "application/ics"):
            continue
        try:
            ical_text = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            continue

        # Only look inside VEVENT blocks — VTIMEZONE also has DTSTART entries (epoch dates)
        vevent = re.search(r"BEGIN:VEVENT(.*?)END:VEVENT", ical_text, re.DOTALL)
        if not vevent:
            continue
        vevent_text = vevent.group(1)

        # DTSTART;TZID=America/Chicago:20260604T163000  or  DTSTART:20260610T140000Z
        m = re.search(r"^DTSTART(?:;[^:]+)?:(\d{8}T\d{6})(Z?)", vevent_text, re.MULTILINE)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
                if m.group(2) == "Z":
                    dt = dt.replace(tzinfo=timezone.utc).replace(tzinfo=None)
                return dt
            except Exception:
                pass

        # All-day: DTSTART;VALUE=DATE:20260610
        m = re.search(r"^DTSTART;VALUE=DATE:(\d{8})", vevent_text, re.MULTILINE)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y%m%d")
            except Exception:
                pass
    return None


# Sentence patterns that strongly suggest a scheduled time
_DATE_SENTENCE_RE = re.compile(
    r"(?:scheduled|confirmed|set|booked|invite[d]?|meet(?:ing)?|interview|call|session)"
    r".{0,120}?"
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}"
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    r"|(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\w*\s+\w+\s+\d{1,2})"
    r".{0,60}?(?:\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))",
    re.IGNORECASE | re.DOTALL,
)


def _extract_body_date(text: str) -> datetime | None:
    """Try to parse a scheduled interview datetime from subject + body text."""
    from dateutil import parser as du_parser
    from dateutil.parser import ParserError

    now = datetime.utcnow()
    future_limit = datetime(now.year + 1, now.month, now.day)

    def _valid(dt) -> bool:
        return now.replace(hour=0, minute=0) <= dt.replace(tzinfo=None) <= future_limit

    # For short strings (e.g. subject lines), try direct fuzzy parse first
    if len(text) < 120 and any(w in text.lower() for w in
            ["interview", "scheduled", "meeting", "call", "invite"]):
        try:
            dt = du_parser.parse(text, fuzzy=True, dayfirst=False)
            if _valid(dt):
                return dt.replace(tzinfo=None)
        except (ParserError, OverflowError, ValueError):
            pass

    # For longer bodies, gate on sentences that look like scheduled times
    matches = _DATE_SENTENCE_RE.findall(text)
    for snippet in matches[:5]:
        try:
            dt = du_parser.parse(snippet, fuzzy=True, dayfirst=False)
            if _valid(dt):
                return dt.replace(tzinfo=None)
        except (ParserError, OverflowError, ValueError):
            continue
    return None


def _extract_interview_date(msg, body: str, subject: str = "") -> datetime | None:
    """ICS attachment first, then subject alone, then full body."""
    return (
        _extract_ical_date(msg)
        or _extract_body_date(subject)
        or _extract_body_date(body)
    )


def _infer_round(subject: str, body: str) -> str:
    """Guess the interview round from subject/body keywords."""
    text = (subject + " " + body).lower()
    if any(w in text for w in ["final", "last round", "last interview"]):
        return "Final"
    if any(w in text for w in ["onsite", "on-site", "on site", "loop"]):
        return "Onsite"
    if any(w in text for w in ["technical", "coding", "take-home", "take home", "assessment"]):
        return "Technical"
    if any(w in text for w in ["hiring manager", "manager round"]):
        return "Hiring Manager"
    if any(w in text for w in ["phone screen", "phone call", "recruiter", "initial"]):
        return "Phone Screen"
    if any(w in text for w in ["video", "zoom", "google meet", "teams"]):
        return "Video Interview"
    return "Interview"


def _resolve_interview_outcomes(db, app, category: str, exclude_interview_id: int | None = None) -> int:
    """
    Infer past interview round outcomes from a status-changing email, since
    nothing else ever sets Interview.outcome:
      - "interview" (a new/next round invite) implies any earlier, still-open
        round (scheduled in the past, no outcome yet) was passed.
      - "rejection" implies the most recent still-open round was failed.
      - "offer" implies every still-open round was passed.
    Returns the number of interview rows updated.
    """
    from src.api.models import Interview

    now = datetime.utcnow()
    q = db.query(Interview).filter(
        Interview.application_id == app.id,
        Interview.outcome.is_(None),
        Interview.scheduled_at.isnot(None),
        Interview.scheduled_at < now,
    )
    if exclude_interview_id:
        q = q.filter(Interview.id != exclude_interview_id)
    open_rounds = q.order_by(Interview.scheduled_at.desc()).all()
    if not open_rounds:
        return 0

    if category == "rejection":
        open_rounds[0].outcome = "fail"
        return 1

    if category in ("offer", "interview"):
        for iv in open_rounds:
            iv.outcome = "pass"
        return len(open_rounds)

    return 0


def run_email_sync() -> dict:
    """
    Connect to Gmail, read emails from the last 30 days, classify them,
    save new events, and update application statuses.
    Returns a summary dict.
    """
    from sqlalchemy import text
    from src.api.database import SessionLocal
    from src.api.models import Application, Job, StatusHistory, TrackedCompany
    from src.email.classifier import classify, extract_company, extract_job_title, _JOB_SENDER_DOMAINS

    EMAIL    = os.getenv("EMAIL_ADDRESS", "")
    PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").replace(" ", "")
    HOST     = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
    PORT     = int(os.getenv("EMAIL_IMAP_PORT", "993"))

    if not EMAIL or not PASSWORD:
        return {"error": "EMAIL_ADDRESS or EMAIL_APP_PASSWORD not configured"}

    db = SessionLocal()
    summary = {"new_events": 0, "status_updates": 0, "errors": 0, "categories": {}}

    try:
        # ── Already-seen message IDs ─────────────────────────────────────────
        seen_ids = {
            row[0] for row in
            db.execute(text("SELECT message_id FROM email_events")).fetchall()
        }

        # ── Known company names + domains for extraction ─────────────────────
        all_tracked = db.query(TrackedCompany).filter_by(is_active=True).all()
        known_companies = [c.company_name for c in all_tracked]
        known_slugs = {c.ats_slug.lower(): c.company_name for c in all_tracked if c.ats_slug}

        # Build set of employer domains: "stripe.com", "databricks.com", etc.
        # Used to decide whether to save "other" category emails
        tracked_domains: set[str] = set()
        for tc in all_tracked:
            slug = tc.ats_slug.lower().replace("-", "").replace("_", "")
            tracked_domains.add(slug + ".com")
            tracked_domains.add(slug + ".ai")
            tracked_domains.add(slug + ".io")
            if tc.career_url:
                import re as _re
                m = _re.search(r'https?://(?:www\.|jobs\.)?([^/]+)', tc.career_url)
                if m:
                    tracked_domains.add(m.group(1).lower())

        # ── IMAP connect ─────────────────────────────────────────────────────
        mail = imaplib.IMAP4_SSL(HOST, PORT)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX", readonly=True)

        # Search a rolling window so the cutoff never goes stale.
        sync_days = int(os.getenv("EMAIL_SYNC_DAYS", "90"))
        since_date = (datetime.utcnow() - timedelta(days=sync_days)).strftime("%d-%b-%Y")
        status, data = mail.search(None, "SINCE", since_date)
        if status != "OK":
            return {"error": "IMAP search failed"}

        message_ids = data[0].split()
        logger.info(f"Email sync: found {len(message_ids)} emails to scan")

        for num in message_ids:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                msg_id = msg.get("Message-ID", "").strip()
                if not msg_id or msg_id in seen_ids:
                    continue

                subject    = _decode_str(msg.get("Subject", ""))
                from_raw   = _decode_str(msg.get("From", ""))
                from_name, from_addr = parseaddr(from_raw)

                try:
                    received_at = parsedate_to_datetime(msg.get("Date", ""))
                    if received_at.tzinfo:
                        received_at = received_at.astimezone(timezone.utc).replace(tzinfo=None)
                except Exception:
                    received_at = datetime.utcnow()

                body = _get_body(msg)
                category = classify(subject, body, from_addr)

                if category == "other":
                    # Save if sender is from a tracked company domain —
                    # this catches recruiter follow-ups that don't hit keywords
                    sender_domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""
                    sender_base   = sender_domain.replace("www.", "")
                    is_employer_sender = (
                        sender_base in tracked_domains
                        or any(sender_base.endswith("." + d) for d in tracked_domains)
                        or sender_domain in _JOB_SENDER_DOMAINS
                    )
                    if not is_employer_sender:
                        seen_ids.add(msg_id)
                        continue
                    # Falls through with category="other" — saved but no status update

                # LinkedIn message notifications get special extraction
                if category == "linkedin_message":
                    from src.email.classifier import extract_linkedin_sender, extract_linkedin_preview
                    sender_name  = extract_linkedin_sender(subject) or from_name
                    preview      = extract_linkedin_preview(body)
                    company      = sender_name
                    company_src  = "sender_domain"
                    job_title    = None
                    snippet      = re.sub(r'\s+', ' ', preview).strip()[:250]
                else:
                    company, company_src = extract_company(subject, body, from_addr, known_companies, known_slugs)
                    job_title = extract_job_title(subject, body)
                    snippet   = re.sub(r'\s+', ' ', body).strip()[:250]

                # ── Match to existing application ────────────────────────────
                linked_app_id = None
                if category == "linkedin_message":
                    pass  # LinkedIn messages are not tied to applications
                elif company:
                    # Don't link/update status for low-confidence (body-only) matches
                    # on sensitive categories like rejection — this prevents wrong companies
                    # from being linked when company name only appears in email boilerplate
                    if company_src == "body_known" and category in _REQUIRES_CONFIDENT_MATCH:
                        logger.info(
                            f"Skipping low-confidence rejection link: company='{company}' "
                            f"found only in body, subject='{subject[:60]}'"
                        )
                        company = None  # don't link, store event with no company

                if company:
                    apps = (
                        db.query(Application)
                        .join(Job, Application.job_id == Job.id)
                        .filter(Job.company_name.ilike(f"%{company}%"))
                        .all()
                    )
                    if apps:
                        # Prefer the most recently *active* application for this
                        # company over the most recently *created* one — otherwise
                        # a new application at a company silently steals every
                        # subsequent email meant for an older, still-active thread.
                        non_terminal = [a for a in apps if a.status not in ("rejected", "withdrawn")]
                        app = max(non_terminal or apps, key=lambda a: a.updated_at or datetime.min)
                        linked_app_id = app.id

                        new_status = _CATEGORY_TO_STATUS.get(category)
                        if new_status:
                            current_priority = _STATUS_PRIORITY.get(app.status, 0)
                            new_priority     = _STATUS_PRIORITY.get(new_status, 0)
                            # Only advance forward in the pipeline, never go backward
                            # Exception: rejection can always set rejected (terminal state)
                            if new_status == "rejected" or new_priority > current_priority:
                                db.add(StatusHistory(
                                    application_id=app.id,
                                    from_status=app.status,
                                    to_status=new_status,
                                    notes=f"email: {subject[:180]}",
                                ))
                                app.status = new_status
                                summary["status_updates"] += 1
                                logger.info(
                                    f"Updated {company} application → {new_status} "
                                    f"(via email: {subject[:60]})"
                                )

                        # Infer outcomes for prior rounds before any new round is created below —
                        # a next-round invite/rejection/offer is direct evidence of what happened
                        # to whatever round was already scheduled and still unresolved.
                        resolved = _resolve_interview_outcomes(db, app, category)
                        if resolved:
                            logger.info(
                                f"Marked {resolved} interview round(s) for {company} "
                                f"app {app.id} via email ({category})"
                            )

                        # Write timeline event + create Interview row for interview emails
                        if category == "interview":
                            from src.api.routes.applications import _write_timeline_event
                            _write_timeline_event(
                                db, app, "interview_invited",
                                notes=subject[:200], source="email",
                            )

                            interview_dt = _extract_interview_date(msg, body, subject)
                            if interview_dt:
                                round_name = _infer_round(subject, body)
                                # Only create if no existing interview on same day
                                existing = db.execute(text("""
                                    SELECT id FROM interviews
                                    WHERE application_id = :app_id
                                    AND DATE(scheduled_at) = DATE(:dt)
                                """), {"app_id": app.id, "dt": interview_dt}).fetchone()
                                if not existing:
                                    db.execute(text("""
                                        INSERT INTO interviews
                                        (application_id, round, scheduled_at, notes)
                                        VALUES (:app_id, :round, :dt, :notes)
                                    """), {
                                        "app_id": app.id,
                                        "round":  round_name,
                                        "dt":     interview_dt,
                                        "notes":  f"Auto-detected from email: {subject[:200]}",
                                    })
                                    logger.info(
                                        f"Created interview for app {app.id} "
                                        f"({round_name}) on {interview_dt}"
                                    )
                                    _write_timeline_event(
                                        db, app, "interview_scheduled",
                                        notes=f"{round_name}: {subject[:180]}", source="email",
                                    )

                        # Recruiter follow-ups during an active interview pipeline are
                        # a checkpoint worth recording, even though they don't change status
                        elif category == "recruiter" and app.status in (
                            "phone_screen", "interview",
                        ):
                            from src.api.routes.applications import _write_timeline_event
                            _write_timeline_event(
                                db, app, "follow_up_received",
                                notes=subject[:200], source="email",
                            )

                # ── Save event ───────────────────────────────────────────────
                db.execute(text("""
                    INSERT OR IGNORE INTO email_events
                    (message_id, received_at, from_address, from_name, subject,
                     category, company_name, job_title, linked_application_id,
                     snippet, processed_at)
                    VALUES (:mid, :recv, :faddr, :fname, :subj,
                            :cat, :co, :jt, :app_id, :snip, :now)
                """), {
                    "mid":    msg_id,
                    "recv":   received_at,
                    "faddr":  from_addr,
                    "fname":  from_name,
                    "subj":   subject,
                    "cat":    category,
                    "co":     company,
                    "jt":     job_title,
                    "app_id": linked_app_id,
                    "snip":   snippet,
                    "now":    datetime.utcnow(),
                })
                seen_ids.add(msg_id)
                summary["new_events"] += 1
                summary["categories"][category] = summary["categories"].get(category, 0) + 1

                # Commit per-message: a failure on a later email must not roll
                # back/discard everything already processed earlier in this run.
                db.commit()

            except Exception as e:
                logger.debug(f"Error processing email {num}: {e}")
                summary["errors"] += 1
                db.rollback()

        mail.logout()

    except Exception as e:
        logger.error(f"Email sync failed: {e}", exc_info=True)
        summary["error"] = str(e)
    finally:
        db.close()

    logger.info(f"Email sync done: {summary}")
    return summary


def reprocess_stale_events() -> dict:
    """
    Re-classify already-saved category='other' email_events with the current
    classifier, and re-run company extraction for already-classified events
    that never got linked to an application (e.g. a sender domain that only
    resolves via a company's ats_slug, not its display name — fixed after
    these rows were first saved). Re-runs the linking/status/interview/
    timeline side effects either way. Needed because classifier/extraction
    improvements only apply to new mail by default — a message_id already in
    email_events is never re-fetched by run_email_sync.
    """
    from sqlalchemy import text
    from src.api.database import SessionLocal
    from src.api.models import Application, Job, StatusHistory
    from src.email.classifier import classify, extract_company

    EMAIL    = os.getenv("EMAIL_ADDRESS", "")
    PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").replace(" ", "")
    HOST     = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
    PORT     = int(os.getenv("EMAIL_IMAP_PORT", "993"))

    if not EMAIL or not PASSWORD:
        return {"error": "EMAIL_ADDRESS or EMAIL_APP_PASSWORD not configured"}

    db = SessionLocal()
    summary = {"checked": 0, "reclassified": 0, "status_updates": 0, "errors": 0}
    mail = None

    try:
        rows = db.execute(text("""
            SELECT id, message_id, subject, from_address, snippet, company_name
            FROM email_events
            WHERE category = 'other'
            OR (category IN ('interview', 'rejection', 'offer') AND linked_application_id IS NULL)
        """)).fetchall()

        known_companies = [
            c[0] for c in db.execute(text(
                "SELECT company_name FROM tracked_companies WHERE is_active = 1"
            )).fetchall()
        ]
        known_slugs = {
            c[0].lower(): c[1] for c in db.execute(text(
                "SELECT ats_slug, company_name FROM tracked_companies WHERE is_active = 1 AND ats_slug IS NOT NULL"
            )).fetchall()
        }

        for row in rows:
            ev_id, msg_id, subject, from_addr, snippet, old_company = row
            summary["checked"] += 1

            # Cheap pre-filter on stored subject/snippet — only hit IMAP for
            # rows whose category actually changes under the new classifier.
            quick_category = classify(subject or "", snippet or "", from_addr or "")
            if quick_category == "other":
                continue

            try:
                if mail is None:
                    mail = imaplib.IMAP4_SSL(HOST, PORT)
                    mail.login(EMAIL, PASSWORD)
                    mail.select("INBOX", readonly=True)

                status, data = mail.search(None, "HEADER", "Message-ID", msg_id)
                if status != "OK" or not data[0]:
                    continue
                num = data[0].split()[0]
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                body = _get_body(msg)
                category = classify(subject or "", body, from_addr or "")
                if category == "other":
                    continue

                company, company_src = extract_company(subject or "", body, from_addr or "", known_companies, known_slugs)
                company = company or old_company

                linked_app_id = None
                if company:
                    apps = (
                        db.query(Application)
                        .join(Job, Application.job_id == Job.id)
                        .filter(Job.company_name.ilike(f"%{company}%"))
                        .all()
                    )
                    if apps:
                        non_terminal = [a for a in apps if a.status not in ("rejected", "withdrawn")]
                        app = max(non_terminal or apps, key=lambda a: a.updated_at or datetime.min)
                        linked_app_id = app.id

                        new_status = _CATEGORY_TO_STATUS.get(category)
                        if new_status:
                            current_priority = _STATUS_PRIORITY.get(app.status, 0)
                            new_priority     = _STATUS_PRIORITY.get(new_status, 0)
                            if new_status == "rejected" or new_priority > current_priority:
                                db.add(StatusHistory(
                                    application_id=app.id,
                                    from_status=app.status,
                                    to_status=new_status,
                                    notes=f"email (reprocessed): {subject[:180] if subject else ''}",
                                ))
                                app.status = new_status
                                summary["status_updates"] += 1

                        resolved = _resolve_interview_outcomes(db, app, category)
                        if resolved:
                            logger.info(
                                f"Reprocess: marked {resolved} interview round(s) for "
                                f"app {app.id} via email ({category})"
                            )

                        if category == "interview":
                            from src.api.routes.applications import _write_timeline_event
                            _write_timeline_event(
                                db, app, "interview_invited",
                                notes=(subject or "")[:200], source="email",
                            )
                            interview_dt = _extract_interview_date(msg, body, subject or "")
                            if interview_dt:
                                round_name = _infer_round(subject or "", body)
                                existing = db.execute(text("""
                                    SELECT id FROM interviews
                                    WHERE application_id = :app_id
                                    AND DATE(scheduled_at) = DATE(:dt)
                                """), {"app_id": app.id, "dt": interview_dt}).fetchone()
                                if not existing:
                                    db.execute(text("""
                                        INSERT INTO interviews
                                        (application_id, round, scheduled_at, notes)
                                        VALUES (:app_id, :round, :dt, :notes)
                                    """), {
                                        "app_id": app.id,
                                        "round":  round_name,
                                        "dt":     interview_dt,
                                        "notes":  f"Auto-detected from email (reprocessed): {(subject or '')[:180]}",
                                    })
                                    _write_timeline_event(
                                        db, app, "interview_scheduled",
                                        notes=f"{round_name}: {(subject or '')[:180]}", source="email",
                                    )
                        elif category == "recruiter" and app.status in ("phone_screen", "interview"):
                            from src.api.routes.applications import _write_timeline_event
                            _write_timeline_event(
                                db, app, "follow_up_received",
                                notes=(subject or "")[:200], source="email",
                            )

                db.execute(text("""
                    UPDATE email_events
                    SET category = :cat, company_name = :co, linked_application_id = :app_id
                    WHERE id = :ev_id
                """), {"cat": category, "co": company, "app_id": linked_app_id, "ev_id": ev_id})
                db.commit()
                summary["reclassified"] += 1
                logger.info(f"Reprocessed event {ev_id} ('{subject}') -> {category}")

            except Exception as e:
                logger.debug(f"Error reprocessing event {ev_id}: {e}")
                summary["errors"] += 1
                db.rollback()

        if mail is not None:
            mail.logout()

    except Exception as e:
        logger.error(f"Reprocess failed: {e}", exc_info=True)
        summary["error"] = str(e)
    finally:
        db.close()

    logger.info(f"Reprocess done: {summary}")
    return summary
