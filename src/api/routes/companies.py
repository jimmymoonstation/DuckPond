import logging
import re
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.api.database import get_db
from src.api.models import TrackedCompany

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["companies"])

VALID_ATS_TYPES = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "amazon", "custom"}


class CompanyOut(BaseModel):
    id: int
    company_name: str
    ats_type: str
    ats_slug: str
    workday_board: Optional[str]
    workday_wd_ver: Optional[str]
    career_url: Optional[str]
    discovered_from: str
    is_active: bool
    added_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class CompanyCreate(BaseModel):
    company_name: str
    ats_type: str
    ats_slug: str
    workday_board: Optional[str] = None
    workday_wd_ver: Optional[str] = "wd5"
    career_url: Optional[str] = None


class CompanyUpdate(BaseModel):
    company_name: Optional[str] = None
    ats_slug: Optional[str] = None
    workday_board: Optional[str] = None
    workday_wd_ver: Optional[str] = None
    career_url: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("", response_model=list[CompanyOut])
def list_companies(db: Session = Depends(get_db)):
    return db.query(TrackedCompany).order_by(TrackedCompany.ats_type, TrackedCompany.company_name).all()


@router.post("", response_model=CompanyOut, status_code=201)
def create_company(body: CompanyCreate, db: Session = Depends(get_db)):
    if body.ats_type not in VALID_ATS_TYPES:
        raise HTTPException(400, f"Invalid ats_type. Choose from: {VALID_ATS_TYPES}")
    existing = db.query(TrackedCompany).filter_by(ats_type=body.ats_type, ats_slug=body.ats_slug).first()
    if existing:
        raise HTTPException(409, "Company with this ats_type + ats_slug already exists")
    company = TrackedCompany(
        company_name=body.company_name,
        ats_type=body.ats_type,
        ats_slug=body.ats_slug,
        workday_board=body.workday_board,
        workday_wd_ver=body.workday_wd_ver or "wd5",
        career_url=body.career_url,
        discovered_from="manual",
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(company_id: int, body: CompanyUpdate, db: Session = Depends(get_db)):
    company = db.query(TrackedCompany).filter_by(id=company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(company, field, val)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=204)
def delete_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(TrackedCompany).filter_by(id=company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    db.delete(company)
    db.commit()


@router.get("/names")
def list_tracked_names(db: Session = Depends(get_db)):
    """Return all active tracked company names + career_url for the dashboard link check."""
    rows = db.query(TrackedCompany.company_name, TrackedCompany.career_url)\
             .filter_by(is_active=True).all()
    return {r.company_name.lower(): r.career_url for r in rows}


class AutoDiscoverBody(BaseModel):
    company_name: str


@router.post("/auto-discover")
async def auto_discover(body: AutoDiscoverBody, db: Session = Depends(get_db)):
    """
    Try to find a company's ATS and add them to tracked_companies.
    Used by My Shots when the company isn't already tracked.
    """
    name = body.company_name.strip()
    if not name:
        raise HTTPException(400, "company_name required")

    # Already tracked?
    existing = (
        db.query(TrackedCompany)
        .filter(TrackedCompany.company_name.ilike(name), TrackedCompany.is_active == True)
        .first()
    )
    if existing:
        return {"status": "exists", "company": CompanyOut.model_validate(existing).model_dump()}

    probe = await _auto_probe(name)
    if not probe:
        return {"status": "not_found", "message": f"Could not find {name} on any known ATS. Try adding manually via the Companies tab."}

    ats_type, slug = probe["ats_type"], probe["ats_slug"]
    existing2 = db.query(TrackedCompany).filter_by(ats_type=ats_type, ats_slug=slug).first()
    if existing2:
        return {"status": "exists", "company": CompanyOut.model_validate(existing2).model_dump()}

    company = _save_company(db, name.title(), probe)
    return {"status": "added", "company": CompanyOut.model_validate(company).model_dump(),
            "message": f"Added {company.company_name} via {ats_type} — {probe.get('job_count','?')} jobs found."}


# ── Company Portal (smart ingest) ─────────────────────────────────────────────

class IngestBody(BaseModel):
    text: str


_ATS_CAREER_URL = {
    "greenhouse":     "https://job-boards.greenhouse.io/{slug}",
    "lever":          "https://jobs.lever.co/{slug}",
    "ashby":          "https://jobs.ashbyhq.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
}

_WORKDAY_BOARDS = ["External_Careers", "Careers", "External", "jobs", "talent"]
_WORKDAY_VERS   = ["wd5", "wd1", "wd12"]


def _slug_candidates(name: str) -> list[str]:
    base = name.lower().strip()
    for suffix in [" inc", " corp", " llc", " ltd", " co.", " co"]:
        if base.endswith(suffix):
            base = base[: -len(suffix)].strip()
    no_special = re.sub(r"[^a-z0-9 ]", "", base)
    return list(dict.fromkeys([
        re.sub(r"\s+", "", no_special),        # nospaces
        re.sub(r"\s+", "-", no_special),       # hyphen
        re.sub(r"[^a-z0-9]", "", base),        # all non-alnum stripped
        re.sub(r"[^a-z0-9]+", "-", base).strip("-"),  # hyphen, all chars
    ]))


async def _probe_greenhouse(client, slug: str) -> int:
    try:
        r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=6)
        if r.status_code == 200:
            return len(r.json().get("jobs", []))
    except Exception:
        pass
    return 0


async def _probe_lever(client, slug: str) -> int:
    try:
        r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=6)
        if r.status_code == 200:
            d = r.json()
            return len(d) if isinstance(d, list) else 0
    except Exception:
        pass
    return 0


async def _probe_ashby(client, slug: str) -> int:
    try:
        r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=6)
        if r.status_code == 200:
            return len(r.json().get("jobPostings", []))
    except Exception:
        pass
    return 0


async def _probe_smartrecruiters(client, slug: str) -> int:
    try:
        r = await client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", timeout=6)
        if r.status_code == 200:
            return r.json().get("totalFound", 0)
    except Exception:
        pass
    return 0


async def _probe_workday(client, slug: str) -> dict | None:
    import asyncio
    for wd_ver in _WORKDAY_VERS:
        for board in _WORKDAY_BOARDS:
            try:
                r = await client.post(
                    f"https://{slug}.{wd_ver}.myworkdayjobs.com/wday/cxs/{slug}/{board}/jobs",
                    json={"limit": 1, "offset": 0, "searchText": ""},
                    timeout=6,
                )
                if r.status_code == 200 and r.json().get("total", 0) > 0:
                    return {"wd_ver": wd_ver, "board": board, "count": r.json()["total"]}
            except Exception:
                pass
    return None


async def _auto_probe(company_name: str) -> dict | None:
    """Try Greenhouse/Lever/Ashby/SmartRecruiters/Workday for all slug candidates."""
    import httpx, asyncio
    candidates = _slug_candidates(company_name)
    async with httpx.AsyncClient() as client:
        for slug in candidates:
            tasks = {
                "greenhouse":      _probe_greenhouse(client, slug),
                "lever":           _probe_lever(client, slug),
                "ashby":           _probe_ashby(client, slug),
                "smartrecruiters": _probe_smartrecruiters(client, slug),
            }
            results = await asyncio.gather(*tasks.values())
            for ats_type, count in zip(tasks.keys(), results):
                if count > 0:
                    return {"ats_type": ats_type, "ats_slug": slug, "job_count": count}

        # Workday: try each slug (heavier, do last)
        for slug in candidates:
            wd = await _probe_workday(client, slug)
            if wd:
                return {"ats_type": "workday", "ats_slug": slug,
                        "workday_board": wd["board"], "workday_wd_ver": wd["wd_ver"],
                        "job_count": wd["count"]}
    return None


def _save_company(db: Session, company_name: str, probe: dict, career_url: str | None = None) -> TrackedCompany:
    ats_type = probe["ats_type"]
    ats_slug = probe["ats_slug"]
    if not career_url:
        tpl = _ATS_CAREER_URL.get(ats_type)
        if tpl:
            career_url = tpl.format(slug=ats_slug)
        elif ats_type == "workday":
            career_url = f"https://{ats_slug}.{probe.get('workday_wd_ver','wd5')}.myworkdayjobs.com/{probe.get('workday_board','')}"
    company = TrackedCompany(
        company_name=company_name,
        ats_type=ats_type,
        ats_slug=ats_slug,
        workday_board=probe.get("workday_board"),
        workday_wd_ver=probe.get("workday_wd_ver", "wd5") if ats_type == "workday" else None,
        career_url=career_url,
        discovered_from="portal",
        is_active=True,
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.post("/ingest")
async def ingest_company(body: IngestBody, db: Session = Depends(get_db)):
    """Smart ingest: accepts a URL, type:slug, or plain company name."""
    from src.scraper.career_pages import detect_ats_from_url
    text = body.text.strip()
    if not text:
        return {"status": "error", "message": "Empty input."}

    # ── Case 1: URL ──────────────────────────────────────────────────────────
    if text.startswith(("http://", "https://")):
        ats = detect_ats_from_url(text)
        if not ats:
            # Store as custom career page
            from urllib.parse import urlparse
            host = urlparse(text).netloc.lstrip("www.")
            existing = db.query(TrackedCompany).filter_by(ats_type="custom", ats_slug=host).first()
            if existing:
                return {"status": "exists", "message": f"Already tracking {existing.company_name} ({host}).", "company": CompanyOut.model_validate(existing).model_dump()}
            company = TrackedCompany(company_name=host.split(".")[0].title(), ats_type="custom",
                                     ats_slug=host, career_url=text, discovered_from="portal", is_active=True)
            db.add(company); db.commit(); db.refresh(company)
            return {"status": "added", "message": f"Added custom career page: {host}", "company": CompanyOut.model_validate(company).model_dump()}
        slug, ats_type = ats["ats_slug"], ats["ats_type"]
        existing = db.query(TrackedCompany).filter_by(ats_type=ats_type, ats_slug=slug).first()
        if existing:
            return {"status": "exists", "message": f"Already tracking {existing.company_name} ({ats_type}:{slug}).", "company": CompanyOut.model_validate(existing).model_dump()}
        # Guess company name from slug
        company_name = slug.replace("-", " ").title()
        company = _save_company(db, company_name, ats, career_url=text)
        return {"status": "added", "message": f"Added {company.company_name} via {ats_type} ({slug}) — {ats.get('job_count', '?')} jobs found.", "company": CompanyOut.model_validate(company).model_dump()}

    # ── Case 2: explicit type:slug ────────────────────────────────────────────
    if ":" in text and text.split(":")[0].lower() in VALID_ATS_TYPES:
        parts = text.split(":", 1)
        ats_type, rest = parts[0].lower(), parts[1].strip()
        # Workday may be  workday:tenant/board
        workday_board, workday_wd_ver = None, "wd5"
        if ats_type == "workday" and "/" in rest:
            slug, workday_board = rest.split("/", 1)
        else:
            slug = rest
        existing = db.query(TrackedCompany).filter_by(ats_type=ats_type, ats_slug=slug).first()
        if existing:
            return {"status": "exists", "message": f"Already tracking {existing.company_name} ({ats_type}:{slug}).", "company": CompanyOut.model_validate(existing).model_dump()}
        company_name = slug.replace("-", " ").title()
        probe = {"ats_type": ats_type, "ats_slug": slug, "workday_board": workday_board, "workday_wd_ver": workday_wd_ver}
        company = _save_company(db, company_name, probe)
        return {"status": "added", "message": f"Added {company.company_name} ({ats_type}:{slug}).", "company": CompanyOut.model_validate(company).model_dump()}

    # ── Case 3: plain company name → auto-probe ───────────────────────────────
    # Check DB first (by name)
    name_lower = text.lower()
    all_companies = db.query(TrackedCompany).all()
    for c in all_companies:
        if c.company_name.lower() == name_lower:
            return {"status": "exists", "message": f"Already tracking {c.company_name} ({c.ats_type}:{c.ats_slug}).", "company": CompanyOut.model_validate(c).model_dump()}

    probe = await _auto_probe(text)
    if not probe:
        return {"status": "not_found", "message": f"Could not find {text} on Greenhouse, Lever, Ashby, SmartRecruiters, or Workday. Try pasting the job URL directly."}

    ats_type, slug = probe["ats_type"], probe["ats_slug"]
    existing = db.query(TrackedCompany).filter_by(ats_type=ats_type, ats_slug=slug).first()
    if existing:
        return {"status": "exists", "message": f"Already tracking {existing.company_name} ({ats_type}:{slug}).", "company": CompanyOut.model_validate(existing).model_dump()}

    company = _save_company(db, text.title(), probe)
    return {"status": "added", "message": f"Added {company.company_name} ({ats_type}:{slug}) — {probe.get('job_count','?')} jobs found.", "company": CompanyOut.model_validate(company).model_dump()}
