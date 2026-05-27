import json
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from src.api.database import get_db
from src.api.models import Resume
from src.api.schemas import ResumeCreate, ResumeListOut, ResumeOut

UPLOAD_DIR = Path("/opt/job-hunt-partner/uploads/resumes")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/resumes", tags=["resumes"])


@router.get("", response_model=ResumeListOut)
def list_resumes(db: Session = Depends(get_db)):
    resumes = db.query(Resume).order_by(Resume.created_at.desc()).all()
    return ResumeListOut(resumes=resumes)


@router.post("", response_model=ResumeOut, status_code=201)
def create_resume(body: ResumeCreate, db: Session = Depends(get_db)):
    resume = Resume(
        name=body.name,
        version=body.version,
        tags=json.dumps(body.tags),
        content_json=json.dumps(body.content_json),
        file_path=body.file_path,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


@router.post("/upload", response_model=ResumeOut, status_code=201)
async def upload_resume(
    file: UploadFile = File(...),
    name: str = Form(...),
    version: str = Form(None),
    tags: str = Form(""),          # comma-separated
    db: Session = Depends(get_db),
):
    allowed = {".pdf", ".doc", ".docx"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"File type {ext} not allowed. Upload PDF or DOCX.")

    # Save file: {resume_id will be unknown until after insert, use temp name first}
    # We'll create the DB record first to get the ID, then rename.
    resume = Resume(
        name=name,
        version=version or None,
        tags=json.dumps([t.strip() for t in tags.split(",") if t.strip()]),
        content_json="{}",
        file_path=None,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    safe_name = f"{resume.id}{ext}"
    dest = UPLOAD_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    resume.file_path = str(dest)
    db.commit()
    db.refresh(resume)
    return resume


@router.get("/{resume_id}/file")
def download_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume or not resume.file_path:
        raise HTTPException(404, "File not found")
    path = Path(resume.file_path)
    if not path.exists():
        raise HTTPException(404, "File missing from disk")
    filename = f"{resume.name}{path.suffix}"
    return FileResponse(str(path), filename=filename, media_type="application/octet-stream")


@router.get("/{resume_id}", response_model=ResumeOut)
def get_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    return resume


@router.put("/{resume_id}", response_model=ResumeOut)
def update_resume(resume_id: int, body: ResumeCreate, db: Session = Depends(get_db)):
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    resume.name = body.name
    resume.version = body.version
    resume.tags = json.dumps(body.tags)
    resume.content_json = json.dumps(body.content_json)
    resume.file_path = body.file_path
    db.commit()
    db.refresh(resume)
    return resume


@router.delete("/{resume_id}", status_code=204)
def delete_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    if resume.file_path:
        try:
            os.remove(resume.file_path)
        except FileNotFoundError:
            pass
    db.delete(resume)
    db.commit()
