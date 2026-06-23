import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api.database import init_db
from src.api.routes import analyze, api_usage, applications, calendar, companies, config, discord_history, interview_prep, jobs, learning, mailbox, notion, portal, resumes, scraper, stats, system_health, timeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Job Hunt Partner API")
    init_db()

    from src.api.scheduler import start_scheduler, stop_scheduler
    start_scheduler()

    yield

    stop_scheduler()
    logger.info("Shutdown complete")


app = FastAPI(
    title="DuckPond 🦆",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
for router in [jobs.router, applications.router, resumes.router, config.router, stats.router, scraper.router, discord_history.router, analyze.router, companies.router, learning.router, mailbox.router, portal.router, timeline.router, interview_prep.router, notion.router, api_usage.router, calendar.router, system_health.router]:
    app.include_router(router, prefix="/api")

# Health check
@app.get("/health")
def health():
    return {"status": "ok"}
