import json
import logging
import subprocess
import textwrap
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.api.database import get_db
from src.api.models import Resume

logger = logging.getLogger(__name__)

def _find_claude_bin() -> str:
    import os, glob, shutil
    # 1. explicit env override
    if (v := os.getenv("CLAUDE_BIN")):
        return v
    # 2. npm-installed claude on PATH for claudebot
    if (p := shutil.which("claude", path="/home/claudebot/.local/bin:/home/claudebot/bin:/usr/local/bin:/usr/bin")):
        return p
    # 3. VSCode Server extension (any version)
    matches = sorted(glob.glob("/home/claudebot/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    if matches:
        return matches[-1]
    # 4. root VSCode Server fallback
    matches = sorted(glob.glob("/root/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    if matches:
        return matches[-1]
    return "claude"  # last resort: hope it's on PATH


CLAUDE_BIN = _find_claude_bin()

router = APIRouter(prefix="/analyze", tags=["analyze"])


class AnalyzeRequest(BaseModel):
    url: str
    page_content: Optional[str] = None   # sent by extension; skips server-side fetch
    original_url: Optional[str] = None   # company's own ATS URL detected by extension


class AnalyzeResult(BaseModel):
    job_title: str
    company_name: str
    location: Optional[str]
    level: Optional[str]
    fit_score: int
    summary: str
    strengths: list[str]
    gaps: list[str]
    cover_letter_bullets: list[str]
    description: Optional[str]
    original_url: Optional[str] = None   # passed through so the extension can use it on save


@router.post("", response_model=AnalyzeResult)
async def analyze_job(body: AnalyzeRequest, db: Session = Depends(get_db)):
    # 1. Use content sent by extension, or fall back to server-side fetch
    if body.page_content and len(body.page_content.strip()) > 100:
        job_text = body.page_content[:8000]
    else:
        job_text = await _fetch_page(body.url)
    if not job_text:
        raise HTTPException(status_code=422, detail="Could not read the job page.")

    # 2. Get the most recent resume
    resume = db.query(Resume).order_by(Resume.created_at.desc()).first()
    resume_text = _format_resume(resume) if resume else "No resume on file yet."

    # 3. Ask Claude to analyze
    result = _call_claude(job_text, resume_text, body.url)
    result.original_url = body.original_url  # pass through for the extension to use on save
    return result


async def _fetch_page(url: str) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "lxml")

            # Remove noise
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()

            # Try to find the main job content block
            for selector in ["#job-description", ".job-description", "[data-testid='job-description']",
                              "main", "article", ".posting-description", "#job-details"]:
                block = soup.select_one(selector)
                if block:
                    text = block.get_text(separator="\n", strip=True)
                    if len(text) > 200:
                        return text[:6000]

            # Fall back to full page text
            return soup.get_text(separator="\n", strip=True)[:6000]
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


def _format_resume(resume: Resume) -> str:
    if not resume:
        return "No resume on file."
    try:
        content = json.loads(resume.content_json or "{}")
    except Exception:
        content = {}

    if not content:
        return f"Resume on file: {resume.name}"

    lines = [f"Resume: {resume.name}"]
    if summary := content.get("summary"):
        lines.append(f"Summary: {summary}")
    for exp in content.get("experience", []):
        lines.append(f"- {exp.get('title')} at {exp.get('company')} ({exp.get('start')}–{exp.get('end', 'present')})")
        for b in exp.get("bullets", []):
            lines.append(f"  • {b}")
    for edu in content.get("education", []):
        lines.append(f"- {edu.get('degree')} from {edu.get('school')} ({edu.get('year')})")
    skills = content.get("skills", [])
    if skills:
        lines.append(f"Skills: {', '.join(skills)}")
    return "\n".join(lines)


def _call_claude(job_text: str, resume_text: str, url: str) -> AnalyzeResult:
    prompt = textwrap.dedent(f"""
        You are a job application consultant. Analyze this job posting against the candidate's resume.

        JOB POSTING (from {url}):
        {job_text}

        CANDIDATE RESUME:
        {resume_text}

        Respond with ONLY a valid JSON object — no markdown, no explanation, just the JSON:
        {{
          "job_title": "exact job title from posting",
          "company_name": "company name",
          "location": "location or null",
          "level": "seniority level or null",
          "fit_score": <0-100 integer — how well the candidate fits>,
          "summary": "2-sentence honest assessment of fit",
          "strengths": ["3-5 specific things from their resume that match this role"],
          "gaps": ["2-4 requirements they may be missing or need to address"],
          "cover_letter_bullets": ["3 punchy first-person bullets for a cover letter, each under 25 words, specific to this role"],
          "description": "first 300 chars of job description for reference"
        }}

        If no resume is on file, set fit_score to 50, leave strengths empty, and note resume is needed.
    """).strip()

    try:
        env = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_API_KEY"}
        result = subprocess.run(
            ["runuser", "-u", "claudebot", "--", CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=60,
            env=env,
        )
        raw = result.stdout.strip()

        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])

        return AnalyzeResult(
            job_title=data.get("job_title", "Unknown Role"),
            company_name=data.get("company_name", "Unknown Company"),
            location=data.get("location"),
            level=data.get("level"),
            fit_score=int(data.get("fit_score", 50)),
            summary=data.get("summary", ""),
            strengths=data.get("strengths", []),
            gaps=data.get("gaps", []),
            cover_letter_bullets=data.get("cover_letter_bullets", []),
            description=data.get("description"),
        )
    except Exception as e:
        logger.error(f"Claude analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")
