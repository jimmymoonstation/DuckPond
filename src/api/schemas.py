import json
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, field_validator


# ── Jobs ──────────────────────────────────────────────────────────────────────

class JobOut(BaseModel):
    id: int
    company_job_id: str
    company_name: str
    job_title: str
    location: Optional[str]
    level: Optional[str]
    url: str
    original_url: Optional[str]
    source: str
    description: Optional[str]
    posted_at: Optional[datetime]
    discovered_at: datetime
    is_active: bool

    model_config = {"from_attributes": True}


class JobListOut(BaseModel):
    total: int
    jobs: list[JobOut]


# ── Resumes ───────────────────────────────────────────────────────────────────

class ResumeCreate(BaseModel):
    name: str
    version: Optional[str] = None
    tags: list[str] = []
    content_json: dict[str, Any] = {}
    file_path: Optional[str] = None


class ResumeOut(BaseModel):
    id: int
    name: str
    version: Optional[str]
    tags: list[str]
    content_json: dict[str, Any]
    file_path: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("content_json", mode="before")
    @classmethod
    def parse_content(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


class ResumeListOut(BaseModel):
    resumes: list[ResumeOut]


# ── Applications ──────────────────────────────────────────────────────────────

class ApplicationCreate(BaseModel):
    job_id: int
    resume_id: Optional[int] = None
    status: str = "applied"
    notes: Optional[str] = None


class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    resume_id: Optional[int] = None
    notes: Optional[str] = None


class StatusHistoryOut(BaseModel):
    id: int
    from_status: Optional[str]
    to_status: str
    changed_at: datetime
    notes: Optional[str]

    model_config = {"from_attributes": True}


class InterviewOut(BaseModel):
    id: int
    round: str
    scheduled_at: Optional[datetime]
    notes: Optional[str]
    outcome: Optional[str]
    prep_notes: Optional[str]

    model_config = {"from_attributes": True}


class InterviewCreate(BaseModel):
    round: str
    scheduled_at: Optional[datetime] = None
    notes: Optional[str] = None


class InterviewUpdate(BaseModel):
    scheduled_at: Optional[datetime] = None
    notes: Optional[str] = None
    outcome: Optional[str] = None
    prep_notes: Optional[str] = None


class ApplicationOut(BaseModel):
    id: int
    job: JobOut
    status: str
    applied_at: Optional[datetime]
    updated_at: datetime
    notes: Optional[str]
    resume: Optional[ResumeOut]
    interviews: list[InterviewOut]

    model_config = {"from_attributes": True}


class ApplicationListOut(BaseModel):
    total: int
    applications: list[ApplicationOut]


# ── Search Config ─────────────────────────────────────────────────────────────

class SearchConfigIn(BaseModel):
    titles: list[str] = []
    locations: list[str] = []
    levels: list[str] = []
    keywords: list[str] = []
    excluded_companies: list[str] = []


class SearchConfigOut(BaseModel):
    id: int
    titles: list[str]
    locations: list[str]
    levels: list[str]
    keywords: list[str]
    excluded_companies: list[str]
    is_active: bool
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("titles", "locations", "levels", "keywords", "excluded_companies", mode="before")
    @classmethod
    def parse_json_list(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v

    # map DB column names → schema field names
    @classmethod
    def from_orm_config(cls, obj):
        return cls(
            id=obj.id,
            titles=json.loads(obj.titles_json),
            locations=json.loads(obj.locations_json),
            levels=json.loads(obj.levels_json),
            keywords=json.loads(obj.keywords_json),
            excluded_companies=json.loads(obj.excluded_companies_json),
            is_active=obj.is_active,
            updated_at=obj.updated_at,
        )


# ── Stats ─────────────────────────────────────────────────────────────────────

class StatusCounts(BaseModel):
    saved: int = 0
    applied: int = 0
    phone_screen: int = 0
    interview: int = 0
    offer: int = 0
    rejected: int = 0
    withdrawn: int = 0


class StatsOut(BaseModel):
    period: str
    jobs_discovered: int
    jobs_active: int
    applications: StatusCounts
    applications_total: int
    interviews_scheduled: int
    days_since_start: int
    days_remaining: int
    daily_average_applications: float
    target_daily_applications: int = 3


# ── Scraper ───────────────────────────────────────────────────────────────────

class ScraperStatusOut(BaseModel):
    last_run: Optional[datetime]
    jobs_found_last_run: int
    total_runs: int
    errors_last_run: int
    is_running: bool
