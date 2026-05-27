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
    """Companies the scraper has learned about — seeded by hand or discovered from manual job adds."""
    __tablename__ = "tracked_companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_name = Column(String, nullable=False)
    ats_type = Column(String, nullable=False)   # greenhouse|lever|ashby|workday|google|custom
    ats_slug = Column(String, nullable=False)    # slug or domain key
    workday_board = Column(String)               # board path for Workday companies
    career_url = Column(String)                  # base career site URL for custom/unknown ATS
    discovered_from = Column(String, nullable=False, default="manual")  # manual|auto
    added_at = Column(DateTime, default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)


class DiscordSession(Base):
    __tablename__ = "discord_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String, nullable=False, unique=True)
    message_history_json = Column(Text, nullable=False, default="[]")
    last_active = Column(DateTime, default=func.now(), nullable=False)
