import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////opt/job-hunt-partner/jobs.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from src.api import models  # noqa: F401 — registers all models
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    _seed_default_config()


def _migrate_db():
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(applications)"))
        columns = {row[1] for row in result}
        if "prep_notes" not in columns:
            conn.execute(text("ALTER TABLE applications ADD COLUMN prep_notes TEXT"))
            conn.commit()
        if "notion_page_id" not in columns:
            conn.execute(text("ALTER TABLE applications ADD COLUMN notion_page_id TEXT"))
            conn.commit()

    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(notion_config)"))
        columns = {row[1] for row in result}
        if "interviews_parent_page_id" not in columns:
            conn.execute(text("ALTER TABLE notion_config ADD COLUMN interviews_parent_page_id TEXT"))
            conn.commit()

        # Seed a single NotionConfig row if the table is empty
    with engine.connect() as conn:
        from src.api.models import NotionConfig  # noqa: F401 — ensure table exists
        result = conn.execute(text("SELECT COUNT(*) FROM notion_config"))
        if result.scalar() == 0:
            conn.execute(text("INSERT INTO notion_config (context_page_ids, is_enabled) VALUES ('[]', 0)"))
            conn.commit()


def _seed_default_config():
    with SessionLocal() as db:
        from src.api.models import SearchConfig
        if not db.query(SearchConfig).first():
            db.add(SearchConfig(
                titles_json="[]",
                locations_json="[]",
                levels_json="[]",
                keywords_json="[]",
                excluded_companies_json="[]",
                is_active=True,
            ))
            db.commit()
