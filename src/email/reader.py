"""
IMAP reader — fetches new job-related emails from Gmail and saves them
as email_events, updating application statuses when matched.
"""
import email
import imaplib
import logging
import os
import re
from datetime import datetime, timezone
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
    """Extract plain-text body from email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body += part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    pass
                if len(body) > 3000:
                    break
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            pass
    return body[:3000]


def run_email_sync() -> dict:
    """
    Connect to Gmail, read emails from the last 30 days, classify them,
    save new events, and update application statuses.
    Returns a summary dict.
    """
    from sqlalchemy import text
    from src.api.database import SessionLocal
    from src.api.models import Application, Job, TrackedCompany
    from src.email.classifier import classify, extract_company, extract_job_title

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
        mail.select("INBOX")

        # Search last 60 days
        status, data = mail.search(None, "SINCE", "01-Apr-2026")
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
                    company, company_src = extract_company(subject, body, from_addr, known_companies)
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
                        linked_app_id = apps[-1].id

                        new_status = _CATEGORY_TO_STATUS.get(category)
                        if new_status:
                            app = apps[-1]
                            current_priority = _STATUS_PRIORITY.get(app.status, 0)
                            new_priority     = _STATUS_PRIORITY.get(new_status, 0)
                            # Only advance forward in the pipeline, never go backward
                            # Exception: rejection can always set rejected (terminal state)
                            if new_status == "rejected" or new_priority > current_priority:
                                app.status = new_status
                                summary["status_updates"] += 1
                                logger.info(
                                    f"Updated {company} application → {new_status} "
                                    f"(via email: {subject[:60]})"
                                )

                        # Write timeline event for interview invitations detected by email
                        if category == "interview":
                            from src.api.routes.applications import _write_timeline_event
                            _write_timeline_event(
                                db, app, "interview_invited",
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

            except Exception as e:
                logger.debug(f"Error processing email {num}: {e}")
                summary["errors"] += 1

        db.commit()
        mail.logout()

    except Exception as e:
        logger.error(f"Email sync failed: {e}", exc_info=True)
        summary["error"] = str(e)
    finally:
        db.close()

    logger.info(f"Email sync done: {summary}")
    return summary
