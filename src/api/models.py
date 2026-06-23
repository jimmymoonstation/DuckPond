from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import relationship
from src.api.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_job_id = Column(String, nullable=False)
    company_name = Column(String, nullable=False)
    job_title = Column(String, nullable=False)
    location = Column(String)
    level = Column(String)
    url = Column(String, nullable=False)          # source URL (e.g. LinkedIn)
    original_url = Column(String)                 # company's own ATS/career page URL
    source = Column(String, nullable=False)
    description = Column(Text)
    posted_at = Column(DateTime)
    discovered_at = Column(DateTime, default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    user_feedback = Column(Text)
    feedback_at = Column(DateTime)
    tags = Column(Text)              # JSON array e.g. ["startup","yc","w24"]

    applications = relationship("Application", back_populates="job")


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    status = Column(String, nullable=False, default="applied")
    applied_at = Column(DateTime)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    notes = Column(Text)

    prep_notes = Column(Text)    # JSON: AI-generated interview prep, keyed by section
    notion_page_id = Column(String)  # Notion page ID auto-created when status → interview

    job = relationship("Job", back_populates="applications")
    resume = relationship("Resume", back_populates="applications")
    status_history = relationship("StatusHistory", back_populates="application", cascade="all, delete-orphan")
    interviews = relationship("Interview", back_populates="application", cascade="all, delete-orphan")


class StatusHistory(Base):
    __tablename__ = "status_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    from_status = Column(String)
    to_status = Column(String, nullable=False)
    changed_at = Column(DateTime, default=func.now(), nullable=False)
    notes = Column(Text)

    application = relationship("Application", back_populates="status_history")


class Interview(Base):
    __tablename__ = "interviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    round = Column(String, nullable=False)
    scheduled_at = Column(DateTime)
    notes = Column(Text)
    outcome = Column(String)
    prep_notes = Column(Text)

    application = relationship("Application", back_populates="interviews")


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    version = Column(String)
    tags = Column(Text, default="[]")        # JSON array
    content_json = Column(Text, default="{}") # JSON document
    file_path = Column(String)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    applications = relationship("Application", back_populates="resume")


class SearchConfig(Base):
    __tablename__ = "search_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    titles_json = Column(Text, nullable=False, default="[]")
    locations_json = Column(Text, nullable=False, default="[]")
    levels_json = Column(Text, nullable=False, default="[]")
    keywords_json = Column(Text, nullable=False, default="[]")
    excluded_companies_json = Column(Text, nullable=False, default="[]")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class TrackedCompany(Base):
    """Master list of companies the scraper targets. Fully manageable from the dashboard."""
    __tablename__ = "tracked_companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_name = Column(String, nullable=False)
    ats_type = Column(String, nullable=False)        # greenhouse|lever|ashby|workday|smartrecruiters|amazon|custom
    ats_slug = Column(String, nullable=False)         # slug, tenant name, or domain key
    workday_board = Column(String)                    # board path for Workday companies
    workday_wd_ver = Column(String, default="wd5")    # Workday data-center version (wd1/wd5/wd12)
    career_url = Column(String)                       # canonical career homepage URL
    discovered_from = Column(String, nullable=False, default="manual")  # manual|seed|auto
    added_at = Column(DateTime, default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)


class NotionConfig(Base):
    __tablename__ = "notion_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_token = Column(String)                        # secret_xxx integration token
    interviews_parent_page_id = Column(String)        # Notion page under which interview pages are created
    context_page_ids = Column(Text, default="[]")     # JSON array: extra pages fed to Claude as context
    tracker_db_id = Column(String)                    # legacy: optional DB sync
    is_enabled = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class DiscordSession(Base):
    __tablename__ = "discord_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String, nullable=False, unique=True)
    message_history_json = Column(Text, nullable=False, default="[]")
    last_active = Column(DateTime, default=func.now(), nullable=False)


class SystemHealthReport(Base):
    """Periodic agent-authored system health check — see src/api/routes/system_health.py."""
    __tablename__ = "system_health_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    overall_status = Column(String, nullable=False)   # ok | warn | critical
    summary = Column(Text, nullable=False)             # agent's human-readable assessment
    components_json = Column(Text, nullable=False)     # [{name, status, message}, ...]
    diagnostics_json = Column(Text)                    # raw diagnostics snapshot the agent reviewed
