import json
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from src.api.database import get_db
from src.api.models import SearchConfig
from src.api.schemas import SearchConfigIn, SearchConfigOut

router = APIRouter(prefix="/config", tags=["config"])


@router.get("", response_model=SearchConfigOut)
def get_config(db: Session = Depends(get_db)):
    cfg = db.query(SearchConfig).filter_by(is_active=True).first()
    return SearchConfigOut.from_orm_config(cfg)


@router.put("", response_model=SearchConfigOut)
def update_config(body: SearchConfigIn, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    cfg = db.query(SearchConfig).filter_by(is_active=True).first()
    cfg.titles_json = json.dumps(body.titles)
    cfg.locations_json = json.dumps(body.locations)
    cfg.levels_json = json.dumps(body.levels)
    cfg.keywords_json = json.dumps(body.keywords)
    cfg.excluded_companies_json = json.dumps(body.excluded_companies)
    cfg.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(cfg)

    from src.scraper.engine import run_scraper
    background_tasks.add_task(run_scraper)

    return SearchConfigOut.from_orm_config(cfg)
