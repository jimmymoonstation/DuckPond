from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.database import get_db

router = APIRouter(prefix="/timeline", tags=["timeline"])

VALID_EVENTS = {
    "applied", "phone_screen",
    "assessment_sent", "assessment_submitted",
    "interview_invited", "interview_scheduled", "interview_completed",
    "follow_up_received",
    "offer_received", "rejected", "withdrawn",
}


class TimelineEventIn(BaseModel):
    application_id: int
    event_type: str
    event_date: Optional[str] = None   # ISO datetime; defaults to now
    notes: Optional[str] = None
    source: Optional[str] = "manual"


@router.post("", status_code=201)
def add_event(body: TimelineEventIn, db: Session = Depends(get_db)):
    if body.event_type not in VALID_EVENTS:
        raise HTTPException(422, f"Invalid event_type. Choose from: {sorted(VALID_EVENTS)}")

    event_date = body.event_date or datetime.utcnow().isoformat()

    # Resolve job info
    row = db.execute(text("""
        SELECT a.job_id, j.company_name, j.job_title,
               a.applied_at
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        WHERE a.id = :id
    """), {"id": body.application_id}).fetchone()
    if not row:
        raise HTTPException(404, "Application not found")
    job_id, company, title, applied_at = row

    # Compute days_since_applied
    days = None
    if body.event_type != "applied" and applied_at:
        try:
            days = max(0, (datetime.fromisoformat(event_date) -
                           datetime.fromisoformat(applied_at)).days)
        except Exception:
            pass

    db.execute(text("""
        INSERT OR IGNORE INTO application_timeline
        (application_id, job_id, company_name, job_title,
         event_type, event_date, days_since_applied, notes, source)
        VALUES (:app_id, :job_id, :company, :title,
                :etype, :edate, :days, :notes, :source)
    """), {
        "app_id":  body.application_id,
        "job_id":  job_id,
        "company": company,
        "title":   title,
        "etype":   body.event_type,
        "edate":   event_date,
        "days":    days,
        "notes":   body.notes,
        "source":  body.source or "manual",
    })
    db.commit()
    return {"ok": True, "days_since_applied": days}


@router.get("/application/{app_id}")
def get_app_timeline(app_id: int, db: Session = Depends(get_db)):
    """Full event history for one application."""
    rows = db.execute(text("""
        SELECT id, event_type, event_date, days_since_applied, notes, source
        FROM application_timeline
        WHERE application_id = :id
        ORDER BY event_date ASC
    """), {"id": app_id}).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/stats")
def timeline_stats(db: Session = Depends(get_db)):
    """Aggregate analytics: avg days to each milestone."""
    rows = db.execute(text("""
        SELECT
            COUNT(DISTINCT application_id)          AS total_applications,
            COUNT(DISTINCT CASE WHEN event_type='interview_invited'   THEN application_id END) AS got_interview_invite,
            COUNT(DISTINCT CASE WHEN event_type='interview_scheduled' THEN application_id END) AS got_interview,
            COUNT(DISTINCT CASE WHEN event_type='offer_received'      THEN application_id END) AS got_offer,
            COUNT(DISTINCT CASE WHEN event_type='rejected'            THEN application_id END) AS rejected,
            ROUND(AVG(CASE WHEN event_type='interview_invited'   THEN days_since_applied END), 1) AS avg_days_to_invite,
            ROUND(AVG(CASE WHEN event_type='interview_scheduled' THEN days_since_applied END), 1) AS avg_days_to_interview,
            ROUND(AVG(CASE WHEN event_type='offer_received'      THEN days_since_applied END), 1) AS avg_days_to_offer,
            ROUND(AVG(CASE WHEN event_type='rejected'            THEN days_since_applied END), 1) AS avg_days_to_rejection
        FROM application_timeline
        WHERE event_type != 'saved'
    """)).fetchone()
    return dict(rows._mapping)


@router.get("/funnel")
def funnel(db: Session = Depends(get_db)):
    """Pivot view: one row per application with all milestone dates."""
    rows = db.execute(text("""
        SELECT application_id, company_name, job_title,
               applied_date, interview_invited_date, interview_scheduled_date,
               interview_completed_date, phone_screen_date,
               offer_date, rejected_date, withdrawn_date,
               days_to_interview_invite, days_to_offer, days_to_rejection
        FROM application_funnel
        ORDER BY applied_date DESC
        LIMIT 200
    """)).fetchall()
    return [dict(r._mapping) for r in rows]
