import hashlib
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from src.api.database import get_db
from src.api.models import Job
from src.api.schemas import JobListOut, JobOut


class JobCreate(BaseModel):
    job_title: str
    company_name: str
    url: str
    original_url: Optional[str] = None   # company's own ATS URL (e.g. from LinkedIn redirect)
    location: Optional[str] = None
    level: Optional[str] = None
    description: Optional[str] = None
    posted_at: Optional[datetime] = None

router = APIRouter(prefix="/jobs", tags=["jobs"])


_SORTABLE = {
    "job_title":    Job.job_title,
    "company_name": Job.company_name,
    "location":     Job.location,
    "level":        Job.level,
    "source":       Job.source,
    "posted_at":    Job.posted_at,
    "discovered_at": Job.discovered_at,
}

@router.get("", response_model=JobListOut)
def list_jobs(
    status: Optional[str] = Query(None, description="new|saved|applied|all"),
    since: Optional[datetime] = None,
    q: Optional[str] = None,
    location: Optional[str] = None,
    level: Optional[str] = None,
    sort_by: Optional[str] = Query("discovered_at", description="column to sort by"),
    sort_dir: Optional[str] = Query("desc", description="asc|desc"),
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    from src.api.models import Application

    query = db.query(Job).filter(Job.is_active == True)

    if since:
        query = query.filter(Job.discovered_at >= since)
    if q:
        query = query.filter(
            Job.job_title.ilike(f"%{q}%") | Job.company_name.ilike(f"%{q}%")
        )
    if location:
        query = query.filter(Job.location.ilike(f"%{location}%"))
    if level:
        query = query.filter(Job.level.ilike(f"%{level}%"))

    # Filter by application status
    if status == "new":
        applied_job_ids = db.query(Application.job_id)
        query = query.filter(Job.id.notin_(applied_job_ids))
    elif status == "applied":
        applied_job_ids = db.query(Application.job_id)
        query = query.filter(Job.id.in_(applied_job_ids))

    total = query.count()
    col = _SORTABLE.get(sort_by, Job.discovered_at)
    order = col.asc() if sort_dir == "asc" else col.desc()
    jobs = query.order_by(order).offset(offset).limit(limit).all()

    return JobListOut(total=total, jobs=jobs)


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("", response_model=JobOut, status_code=201)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    job_id = hashlib.sha256(f"{body.company_name}{body.job_title}{body.url}".encode()).hexdigest()[:16]
    existing = db.query(Job).filter_by(company_job_id=job_id, source="manual").first()
    if existing:
        raise HTTPException(status_code=409, detail="Job already exists")
    job = Job(
        company_job_id=job_id,
        company_name=body.company_name,
        job_title=body.job_title,
        location=body.location,
        level=body.level,
        url=body.url,
        original_url=body.original_url,
        source="manual",
        description=body.description,
        posted_at=body.posted_at,
        discovered_at=datetime.utcnow(),
        is_active=True,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Learn the company's career site from whichever URL reveals more (prefer original_url)
    learn_url = body.original_url or body.url
    _maybe_register_company(db, body.company_name, learn_url)

    return job


def _maybe_register_company(db: Session, company_name: str, url: str):
    """
    Learn a company's career source from a manually-added job URL.
    - Known ATS (Greenhouse/Lever/Ashby/Workday): register so scraper covers it next run.
    - Unrecognized domain (Google, Meta, custom): store career_url for reference/future scrapers.
    - Skips anything already in hardcoded lists or already tracked.
    """
    from urllib.parse import urlparse
    from src.api.models import TrackedCompany
    from src.scraper.career_pages import (
        detect_ats_from_url,
        GREENHOUSE_COMPANIES, LEVER_COMPANIES, ASHBY_COMPANIES, WORKDAY_COMPANIES,
        KNOWN_CAREER_DOMAINS,
    )

    try:
        ats = detect_ats_from_url(url)

        if ats:
            slug = ats["ats_slug"]
            already_known = (
                (ats["ats_type"] == "greenhouse" and slug in GREENHOUSE_COMPANIES) or
                (ats["ats_type"] == "lever" and slug in LEVER_COMPANIES) or
                (ats["ats_type"] == "ashby" and slug in ASHBY_COMPANIES) or
                (ats["ats_type"] == "workday" and any(slug == t[0] for t in WORKDAY_COMPANIES))
            )
            if already_known:
                return
            existing = db.query(TrackedCompany).filter_by(ats_type=ats["ats_type"], ats_slug=slug).first()
            if existing:
                return
            db.add(TrackedCompany(
                company_name=company_name,
                ats_type=ats["ats_type"],
                ats_slug=slug,
                workday_board=ats.get("workday_board"),
                career_url=url,
                discovered_from="manual",
            ))
            db.commit()
            logger.info(f"Learned new company: {company_name} ({ats['ats_type']}:{slug})")

        else:
            # Unknown ATS — store the career domain so we remember where this company posts
            parsed = urlparse(url)
            host = parsed.netloc.lower().lstrip("www.")
            # Skip generic job boards — we only want company-owned career sites
            skip_hosts = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
                          "monster.com", "dice.com", "simplyhired.com", "wellfound.com"}
            if any(s in host for s in skip_hosts):
                return
            career_base = f"{parsed.scheme}://{parsed.netloc}"
            existing = db.query(TrackedCompany).filter_by(ats_type="custom", ats_slug=host).first()
            if existing:
                return
            db.add(TrackedCompany(
                company_name=company_name,
                ats_type="custom",
                ats_slug=host,
                career_url=career_base,
                discovered_from="manual",
            ))
            db.commit()
            logger.info(f"Learned custom career site: {company_name} → {career_base}")

    except Exception as e:
        logger.debug(f"Could not register company from URL {url}: {e}")


@router.patch("/{job_id}")
def update_job(job_id: int, is_active: Optional[bool] = None, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if is_active is not None:
        job.is_active = is_active
    db.commit()
    return {"ok": True}
