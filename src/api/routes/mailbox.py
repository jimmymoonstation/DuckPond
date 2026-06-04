from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from src.api.database import SessionLocal

router = APIRouter(prefix="/mailbox", tags=["mailbox"])

_VALID_CATEGORIES = {
    "offer", "interview", "assessment", "rejection",
    "application_confirm", "linkedin_message", "recruiter", "other",
}


class EventPatch(BaseModel):
    category: Optional[str] = None
    company_name: Optional[str] = None

_CATEGORY_ICON = {
    "offer":               "🎉",
    "interview":           "📅",
    "assessment":          "📝",
    "rejection":           "❌",
    "application_confirm": "✅",
    "linkedin_message":    "💬",
    "recruiter":           "📨",
    "other":               "📧",
}


@router.get("/summary")
def mailbox_summary():
    """Aggregate metrics + recent events for the Mailbox tab."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT category, COUNT(*) as cnt
            FROM email_events
            GROUP BY category
        """)).fetchall()
        by_category = {r[0]: r[1] for r in rows}

        total = sum(by_category.values())

        # Last 7 days
        since = (datetime.utcnow() - timedelta(days=7)).isoformat()
        week_rows = db.execute(text("""
            SELECT category, COUNT(*) as cnt
            FROM email_events
            WHERE received_at >= :since
            GROUP BY category
        """), {"since": since}).fetchall()
        this_week = {r[0]: r[1] for r in week_rows}

        # Last sync time
        last_sync = db.execute(text(
            "SELECT MAX(processed_at) FROM email_events"
        )).scalar()

        # Recent events (last 50)
        recent = db.execute(text("""
            SELECT e.id, e.received_at, e.from_name, e.from_address,
                   e.subject, e.category, e.company_name, e.job_title,
                   e.linked_application_id, e.snippet
            FROM email_events e
            ORDER BY e.received_at DESC
            LIMIT 50
        """)).fetchall()

        events = []
        for r in recent:
            events.append({
                "id":                   r[0],
                "received_at":          r[1],
                "from_name":            r[2],
                "from_address":         r[3],
                "subject":              r[4],
                "category":             r[5],
                "icon":                 _CATEGORY_ICON.get(r[5], "📧"),
                "company_name":         r[6],
                "job_title":            r[7],
                "linked_application_id": r[8],
                "snippet":              r[9],
            })

        return {
            "total_emails":         total,
            "by_category":          by_category,
            "this_week":            this_week,
            "last_sync":            last_sync,
            "recent_events":        events,
            "linkedin_messages":    by_category.get("linkedin_message", 0),
        }
    finally:
        db.close()


@router.get("/linkedin-messages")
def linkedin_messages(limit: int = 50):
    """Return LinkedIn direct message notifications, newest first."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id, received_at, from_name, from_address,
                   subject, company_name, snippet
            FROM email_events
            WHERE category = 'linkedin_message'
            ORDER BY received_at DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()

        total = db.execute(text(
            "SELECT COUNT(*) FROM email_events WHERE category = 'linkedin_message'"
        )).scalar() or 0

        unread_cutoff = (datetime.utcnow() - timedelta(days=3)).isoformat()
        unread = db.execute(text("""
            SELECT COUNT(*) FROM email_events
            WHERE category = 'linkedin_message' AND received_at >= :since
        """), {"since": unread_cutoff}).scalar() or 0

        messages = []
        for r in rows:
            messages.append({
                "id":           r[0],
                "received_at":  r[1],
                "from_name":    r[2],
                "from_address": r[3],
                "subject":      r[4],
                "sender_name":  r[5],   # stored in company_name field
                "preview":      r[6],   # stored in snippet field
            })

        return {"total": total, "unread_3d": unread, "messages": messages}
    finally:
        db.close()


@router.post("/sync")
def trigger_sync():
    """Manually trigger an email sync."""
    from src.email.reader import run_email_sync
    return run_email_sync()


@router.patch("/events/{event_id}")
def patch_event(event_id: int, body: EventPatch):
    """Manually correct the category or company_name of an email event."""
    if body.category and body.category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Invalid category: {body.category}")

    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT id FROM email_events WHERE id=:id"), {"id": event_id}
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Event not found")

        updates = {}
        if body.category is not None:
            updates["category"] = body.category
        if body.company_name is not None:
            updates["company_name"] = body.company_name.strip() or None

        if updates:
            set_clause = ", ".join(f"{k}=:{k}" for k in updates)
            updates["id"] = event_id
            db.execute(text(f"UPDATE email_events SET {set_clause} WHERE id=:id"), updates)
            db.commit()

        return {"ok": True, "updated": list(updates.keys())}
    finally:
        db.close()
