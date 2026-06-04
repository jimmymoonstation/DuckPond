import json
import logging
import subprocess
import textwrap

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.api.database import get_db
from src.api.models import Application, Job, Resume
from src.api.routes.analyze import CLAUDE_BIN, _format_resume
from src.api.routes.notion import fetch_my_notes_for_app, fetch_notion_context, write_prep_to_notion
from src.api.schemas import ApplicationOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interview-prep", tags=["interview-prep"])

INTERVIEW_STATUSES = {"phone_screen", "interview", "offer"}


@router.get("", response_model=list[ApplicationOut])
def list_interview_applications(db: Session = Depends(get_db)):
    return (
        db.query(Application)
        .join(Job, Application.job_id == Job.id)
        .filter(Application.status.in_(INTERVIEW_STATUSES))
        .order_by(Application.updated_at.desc())
        .all()
    )


@router.post("/{app_id}/generate")
async def generate_interview_prep(app_id: int, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")

    job = db.query(Job).filter(Job.id == app.job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    resume = db.query(Resume).order_by(Resume.created_at.desc()).first()
    resume_text = _format_resume(resume) if resume else "No resume on file."
    job_text = job.description or f"{job.job_title} at {job.company_name}"

    # Pull context: general Notion pages + user's own notes from this interview's page
    notion_context = await fetch_notion_context(db)
    my_notes = await fetch_my_notes_for_app(app.notion_page_id, db) if app.notion_page_id else ""

    prep = _generate_prep(
        job_text, resume_text, job.job_title, job.company_name, job.level,
        notion_context=notion_context,
        my_notes=my_notes,
    )

    # Save to DB
    app.prep_notes = json.dumps(prep)
    db.commit()

    # Write to Notion page if one exists
    if app.notion_page_id:
        await write_prep_to_notion(app.notion_page_id, prep, db)

    return prep


def _generate_prep(
    job_text: str, resume_text: str, job_title: str, company: str, level,
    notion_context: str = "", my_notes: str = "",
) -> dict:
    level_str = f" ({level})" if level else ""

    extra = ""
    if notion_context:
        extra += f"\nCANDIDATE BACKGROUND (from Notion):\n{notion_context[:2000]}\n"
    if my_notes:
        extra += f"\nCANDIDATE'S OWN INTERVIEW NOTES (from their Notion page):\n{my_notes[:1500]}\n"

    prompt = textwrap.dedent(f"""
        You are an expert interview coach. Generate targeted interview preparation for this candidate.

        ROLE: {job_title} at {company}{level_str}

        JOB DESCRIPTION:
        {job_text[:4000]}

        CANDIDATE RESUME:
        {resume_text[:2000]}
        {extra}
        Respond with ONLY a valid JSON object — no markdown, no explanation, just the JSON:
        {{
          "likely_questions": ["5-7 specific technical or role-specific questions likely to be asked for this exact role"],
          "topics_to_study": ["5-8 specific technologies, concepts, or skills to brush up on based on the job description"],
          "company_research": ["4-5 specific things to research or know about {company} before the interview"],
          "behavioral_questions": ["4-5 behavioral STAR-format questions relevant to this role and level"],
          "tips": ["3-4 actionable interview tips tailored to this role, company, and anything noted above"]
        }}
    """).strip()

    try:
        result = subprocess.run(
            ["runuser", "-u", "claudebot", "--", CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=90,
        )
        raw = result.stdout.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON in Claude output: {raw[:200]}")
        return json.loads(raw[start:end])
    except Exception as e:
        logger.error(f"Claude prep generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Prep generation failed: {e}")
