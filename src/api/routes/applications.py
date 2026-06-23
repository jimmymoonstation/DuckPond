from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.database import get_db
from src.api.models import Application, Interview, Job, Resume, StatusHistory
from src.api.routes.notion import auto_create_interview_page, INTERVIEW_STATUSES
from src.api.schemas import (
    ApplicationCreate, ApplicationListOut, ApplicationOut, ApplicationUpdate,
    InterviewCreate, InterviewOut, InterviewUpdate, StatusHistoryOut,
)

_STATUS_TO_EVENT = {
    "applied":      "applied",
    "phone_screen": "phone_screen",
    "interview":    "interview_scheduled",
    "offer":        "offer_received",
    "rejected":     "rejected",
    "withdrawn":    "withdrawn",
}


def _write_timeline_event(db: Session, app: Application, event_type: str,
                          notes: str | None = None, source: str = "manual"):
    """Insert one row into application_timeline for an application status change."""
    job = db.query(Job).filter_by(id=app.job_id).first()
    if not job:
        return
    now = datetime.utcnow()
    days = None
    if event_type != "applied" and app.applied_at:
        days = max(0, (now - app.applied_at).days)
    db.execute(text("""
        INSERT OR IGNORE INTO application_timeline
        (application_id, job_id, company_name, job_title,
         event_type, event_date, days_since_applied, notes, source)
        VALUES (:app_id, :job_id, :company, :title,
                :etype, :edate, :days, :notes, :source)
    """), {
        "app_id":  app.id, "job_id": job.id,
        "company": job.company_name, "title": job.job_title,
        "etype":   event_type, "edate": now.isoformat(),
        "days":    days, "notes": notes, "source": source,
    })

router = APIRouter(prefix="/applications", tags=["applications"])

VALID_STATUSES = {"saved", "applied", "phone_screen", "interview", "offer", "rejected", "withdrawn"}


@router.get("", response_model=ApplicationListOut)
def list_applications(
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # Inner join ensures we only return applications whose job still exists
    query = db.query(Application).join(Job, Application.job_id == Job.id)
    if status:
        query = query.filter(Application.status == status)
    total = query.count()
    apps = query.order_by(Application.updated_at.desc()).offset(offset).limit(limit).all()
    return ApplicationListOut(total=total, applications=apps)


@router.post("", response_model=ApplicationOut, status_code=201)
def create_application(body: ApplicationCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Choose from: {VALID_STATUSES}")
    job = db.query(Job).filter(Job.id == body.job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    app = Application(
        job_id=body.job_id,
        resume_id=body.resume_id,
        status=body.status,
        applied_at=datetime.utcnow() if body.status != "saved" else None,
        notes=body.notes,
        updated_at=datetime.utcnow(),
    )
    db.add(app)
    db.flush()

    db.add(StatusHistory(
        application_id=app.id,
        from_status=None,
        to_status=body.status,
    ))
    db.flush()
    event = _STATUS_TO_EVENT.get(body.status)
    if event:
        _write_timeline_event(db, app, event, body.notes)
    db.commit()
    db.refresh(app)

    if body.status in INTERVIEW_STATUSES:
        background_tasks.add_task(auto_create_interview_page, app.id)

    return app


@router.get("/{app_id}", response_model=ApplicationOut)
def get_application(app_id: int, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@router.patch("/{app_id}", response_model=ApplicationOut)
def update_application(app_id: int, body: ApplicationUpdate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")

    trigger_notion_page = False
    if body.status and body.status != app.status:
        if body.status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status")
        db.add(StatusHistory(
            application_id=app.id,
            from_status=app.status,
            to_status=body.status,
            notes=body.notes,
        ))
        app.status = body.status
        if body.status == "applied" and not app.applied_at:
            app.applied_at = datetime.utcnow()
        event = _STATUS_TO_EVENT.get(body.status)
        if event:
            _write_timeline_event(db, app, event, body.notes)
        if body.status in INTERVIEW_STATUSES and not app.notion_page_id:
            trigger_notion_page = True

    if body.notes is not None:
        app.notes = body.notes
    if body.resume_id is not None:
        app.resume_id = body.resume_id

    app.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(app)

    if trigger_notion_page:
        background_tasks.add_task(auto_create_interview_page, app.id)

    return app


@router.get("/{app_id}/history", response_model=list[StatusHistoryOut])
def get_history(app_id: int, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")
    return app.status_history


@router.post("/{app_id}/interviews", response_model=InterviewOut, status_code=201)
def add_interview(app_id: int, body: InterviewCreate, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")

    interview = Interview(
        application_id=app_id,
        round=body.round,
        scheduled_at=body.scheduled_at,
        notes=body.notes,
    )
    db.add(interview)

    # Auto-advance status to interview if not already further along
    if app.status in ("applied", "phone_screen", "saved"):
        db.add(StatusHistory(
            application_id=app_id,
            from_status=app.status,
            to_status="interview",
        ))
        app.status = "interview"
        app.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(interview)
    return interview


@router.patch("/interviews/{interview_id}", response_model=InterviewOut)
def update_interview(interview_id: int, body: InterviewUpdate, db: Session = Depends(get_db)):
    interview = db.query(Interview).filter(Interview.id == interview_id).first()
    if not interview:
        raise HTTPException(404, "Interview not found")

    had_outcome = interview.outcome is not None
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(interview, field, val)

    # Record a checkpoint the first time this round gets an outcome
    if body.outcome is not None and not had_outcome:
        app = db.query(Application).filter(Application.id == interview.application_id).first()
        if app:
            _write_timeline_event(
                db, app, "interview_completed",
                notes=f"{interview.round}: {body.outcome}", source="manual",
            )

    db.commit()
    db.refresh(interview)
    return interview
