"""
Notion integration — page-based workflow.

Each interview gets its own Notion page under a configured parent page.
Claude synthesizes the job description + recruiter emails into a structured
page with toggles and to-do checkboxes. User's "My Notes" section is never
overwritten — Duck Hunt reads it back as context for future AI calls.
"""

import json
import logging
import subprocess
import textwrap
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from notion_client import AsyncClient, APIResponseError
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.database import SessionLocal, get_db
from src.api.models import Application, NotionConfig
from src.api.routes.analyze import CLAUDE_BIN, _format_resume

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notion", tags=["notion"])

_HEADING_AI_PREP  = "🤖 AI Interview Prep"
_HEADING_MY_NOTES = "✍️ My Notes"
INTERVIEW_STATUSES = {"phone_screen", "interview", "offer"}


# ── Schemas ────────────────────────────────────────────────────────────────────

class NotionConfigIn(BaseModel):
    api_token: Optional[str] = None
    interviews_parent_page_id: Optional[str] = None
    context_page_ids: list[str] = []
    is_enabled: bool = False


class NotionConfigOut(BaseModel):
    api_token_set: bool
    interviews_parent_page_id: Optional[str]
    context_page_ids: list[str]
    is_enabled: bool
    model_config = {"from_attributes": True}


# ── DB / client helpers ────────────────────────────────────────────────────────

def _get_config(db: Session) -> NotionConfig:
    cfg = db.query(NotionConfig).first()
    if not cfg:
        cfg = NotionConfig(context_page_ids="[]", is_enabled=False)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _notion(token: str) -> AsyncClient:
    return AsyncClient(auth=token)


# ── Block helpers ──────────────────────────────────────────────────────────────

def _rt(content: str) -> dict:
    return {"type": "text", "text": {"content": content[:2000]}}


def _h(level: int, content: str) -> dict:
    k = f"heading_{level}"
    return {"object": "block", "type": k, k: {"rich_text": [_rt(content)]}}


def _p(content: str = "") -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [_rt(content)] if content else []}}


def _bullet(content: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [_rt(content)]}}


def _todo(content: str, checked: bool = False) -> dict:
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [_rt(content)], "checked": checked}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _callout(content: str, emoji: str = "💡") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": [_rt(content)], "icon": {"type": "emoji", "emoji": emoji}}}


def _toggle(title: str, children: list) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [_rt(title)],
            "children": children,
        },
    }


# ── Email fetch ────────────────────────────────────────────────────────────────

def _fetch_company_emails(company_name: str, db: Session) -> list[dict]:
    """Fetch relevant emails for a company from email_events."""
    rows = db.execute(text("""
        SELECT subject, from_name, from_address, category, snippet, received_at
        FROM email_events
        WHERE LOWER(company_name) LIKE LOWER(:pattern)
          AND category IN ('interview', 'recruiter', 'offer', 'assessment', 'application_confirm')
        ORDER BY received_at ASC
        LIMIT 10
    """), {"pattern": f"%{company_name}%"}).fetchall()

    return [
        {
            "subject":    r[0] or "",
            "from_name":  r[1] or "",
            "from_email": r[2] or "",
            "category":   r[3] or "",
            "snippet":    r[4] or "",
            "date":       str(r[5])[:10] if r[5] else "",
        }
        for r in rows
    ]


# ── Claude: generate rich structured page content ─────────────────────────────

def _generate_page_content(app: Application, emails: list[dict], resume_text: str) -> dict:
    """Ask Claude to synthesize job desc + emails into structured prep content."""
    job = app.job
    status_labels = {"phone_screen": "Phone Screen", "interview": "Interview", "offer": "Offer 🎉"}

    email_block = ""
    if emails:
        lines = []
        for e in emails:
            lines.append(f"[{e['date']} | {e['category']} | From: {e['from_name']}]")
            lines.append(f"Subject: {e['subject']}")
            lines.append(f"Snippet: {e['snippet'][:400]}")
            lines.append("")
        email_block = "\n".join(lines)

    prompt = textwrap.dedent(f"""
        You are an expert interview coach preparing a candidate for an interview.

        ROLE: {job.job_title} at {job.company_name} ({status_labels.get(app.status, app.status)})
        LOCATION: {job.location or "—"}  |  LEVEL: {job.level or "—"}

        JOB DESCRIPTION:
        {(job.description or "Not available")[:3500]}

        RECRUITER / EMAIL CONTEXT:
        {email_block or "No emails found for this company."}

        CANDIDATE RESUME:
        {resume_text[:1500]}

        Based on everything above, produce a structured JSON prep pack for this interview.
        Respond with ONLY valid JSON — no markdown, no explanation:
        {{
          "role_summary": "2-3 sentence plain-English summary of what this role is about and what they're looking for",
          "email_insights": ["key fact or logistics detail extracted from the emails, e.g. interview format, recruiter name, scheduling notes"],
          "prep_todos": ["specific actionable to-do, e.g. 'Review Apple's latest keynote products', 'Practice system design for large-scale APIs'"],
          "likely_questions": ["specific interview question likely for this exact role"],
          "questions_to_ask": ["thoughtful question the candidate should ask the interviewer"],
          "topics_to_study": ["specific technology, concept, or skill to review"]
        }}

        Rules:
        - prep_todos: 5-8 specific, actionable items as if writing a personal checklist
        - likely_questions: 5-7 specific to this role and company
        - questions_to_ask: 4-5 genuinely useful questions
        - topics_to_study: 5-7 items pulled from the job description
        - email_insights: only include if there were actual emails; otherwise empty list
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
            raise ValueError(f"No JSON: {raw[:200]}")
        return json.loads(raw[start:end])
    except Exception as e:
        logger.error(f"Claude page content generation failed: {e}")
        return {}


# ── Page block builder ─────────────────────────────────────────────────────────

def _build_page_blocks(app: Application, content: dict, emails: list[dict]) -> list:
    """Convert structured content dict into Notion blocks."""
    job = app.job
    status_labels = {"phone_screen": "Phone Screen", "interview": "Interview", "offer": "Offer 🎉"}
    blocks = []

    # ── Role overview callout ──
    summary = content.get("role_summary") or f"{job.job_title} at {job.company_name}"
    blocks.append(_callout(summary, "🎯"))

    # ── Quick facts ──
    facts = []
    if job.location:   facts.append(f"📍 {job.location}")
    if job.level:      facts.append(f"🏷 {job.level}")
    if app.applied_at: facts.append(f"📅 Applied {app.applied_at.date().isoformat()}")
    facts.append(f"🔗 {job.url}")
    facts.append(f"📌 Status: {status_labels.get(app.status, app.status)}")
    blocks += [_p(f) for f in facts]
    blocks.append(_divider())

    # ── Email insights toggle ──
    insights = content.get("email_insights", [])
    if insights or emails:
        email_children = [_bullet(i) for i in insights] if insights else [_p("No insights extracted.")]
        if emails:
            email_children.append(_p(""))
            email_children.append(_p("── Raw emails ──"))
            for e in emails:
                email_children.append(_bullet(f"[{e['date']}] {e['from_name']} — {e['subject']}"))
                if e["snippet"]:
                    email_children.append(_p(f"  {e['snippet'][:300]}"))
        blocks.append(_toggle("📬 Recruiter & Email Thread", email_children))

    # ── Job description toggle ──
    if job.description:
        desc = job.description[:3000]
        desc_children = [_p(chunk) for chunk in [desc[i:i+1900] for i in range(0, len(desc), 1900)]]
        blocks.append(_toggle("📝 Job Description", desc_children))

    blocks.append(_divider())

    # ── Prep to-do list ──
    todos = content.get("prep_todos", [])
    if todos:
        blocks.append(_h(2, "✅ Prep Checklist"))
        blocks += [_todo(t) for t in todos]
        blocks.append(_p(""))

    # ── Likely questions toggle ──
    questions = content.get("likely_questions", [])
    if questions:
        blocks.append(_toggle("❓ Likely Interview Questions", [_bullet(q) for q in questions]))

    # ── Topics to study toggle ──
    topics = content.get("topics_to_study", [])
    if topics:
        blocks.append(_toggle("📚 Topics to Study", [_bullet(t) for t in topics]))

    # ── Questions to ask toggle ──
    ask = content.get("questions_to_ask", [])
    if ask:
        blocks.append(_toggle("💬 Questions to Ask Them", [_bullet(q) for q in ask]))

    blocks.append(_divider())

    # ── My Notes (user-owned, never overwritten) ──
    blocks += [
        _h(2, _HEADING_MY_NOTES),
        _callout("This section is yours — Duck Hunt will never overwrite it.", "✏️"),
        _h(3, "Round Notes"),
        _p("Round 1 — Date: | Format: | Interviewers: | Outcome:"),
        _h(3, "Follow-up Actions"),
        _todo(""),
    ]

    return blocks


# ── Internal page creation ─────────────────────────────────────────────────────

async def _do_create_page(app: Application, cfg: NotionConfig, db: Session) -> str:
    """Create the Notion page and return its ID."""
    from src.api.models import Resume
    resume = db.query(Resume).order_by(Resume.created_at.desc()).first()
    resume_text = _format_resume(resume) if resume else "No resume on file."

    emails = _fetch_company_emails(app.job.company_name, db)
    content = _generate_page_content(app, emails, resume_text)

    status_labels = {"phone_screen": "Phone Screen", "interview": "Interview", "offer": "Offer 🎉"}
    title = f"{app.job.company_name} — {app.job.job_title} [{status_labels.get(app.status, app.status)}]"

    blocks = _build_page_blocks(app, content, emails)

    # Notion API max 100 children per create call — split if needed
    notion = _notion(cfg.api_token)
    page = await notion.pages.create(
        parent={"page_id": cfg.interviews_parent_page_id},
        properties={"title": {"title": [_rt(title)]}},
        children=blocks[:100],
    )
    page_id = page["id"]

    # Append any overflow blocks
    if len(blocks) > 100:
        await notion.blocks.children.append(block_id=page_id, children=blocks[100:])

    return page_id


# ── Config endpoints ───────────────────────────────────────────────────────────

@router.get("/config", response_model=NotionConfigOut)
def get_config(db: Session = Depends(get_db)):
    cfg = _get_config(db)
    return NotionConfigOut(
        api_token_set=bool(cfg.api_token),
        interviews_parent_page_id=cfg.interviews_parent_page_id,
        context_page_ids=json.loads(cfg.context_page_ids or "[]"),
        is_enabled=cfg.is_enabled,
    )


@router.put("/config", response_model=NotionConfigOut)
def save_config(body: NotionConfigIn, db: Session = Depends(get_db)):
    cfg = _get_config(db)
    if body.api_token is not None:
        cfg.api_token = body.api_token.strip() or None
    if body.interviews_parent_page_id is not None:
        cfg.interviews_parent_page_id = _extract_id(body.interviews_parent_page_id) or None
    cfg.context_page_ids = json.dumps(body.context_page_ids)
    cfg.is_enabled = body.is_enabled
    db.commit()
    return NotionConfigOut(
        api_token_set=bool(cfg.api_token),
        interviews_parent_page_id=cfg.interviews_parent_page_id,
        context_page_ids=json.loads(cfg.context_page_ids or "[]"),
        is_enabled=cfg.is_enabled,
    )


@router.get("/test")
async def test_connection(db: Session = Depends(get_db)):
    cfg = _get_config(db)
    if not cfg.api_token:
        raise HTTPException(400, "No API token configured")
    try:
        me = await _notion(cfg.api_token).users.me()
        name = me.get("name") or me.get("bot", {}).get("workspace_name", "Connected")
        return {"ok": True, "name": name}
    except APIResponseError as e:
        raise HTTPException(e.status, str(e))


@router.post("/create-page/{app_id}")
async def create_page_for_app(app_id: int, db: Session = Depends(get_db)):
    """Manually create (or re-create) a Notion interview page for an application."""
    from src.api.models import Application as App
    app = db.query(App).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(404, "Application not found")

    cfg = _get_config(db)
    if not cfg.api_token:
        raise HTTPException(400, "No API token configured")
    if not cfg.interviews_parent_page_id:
        raise HTTPException(400, "No interview pages parent set — configure it in Settings → Notion")
    if not cfg.is_enabled:
        raise HTTPException(400, "Notion integration is disabled — enable it in Settings → Notion")

    try:
        page_id = await _do_create_page(app, cfg, db)
        app.notion_page_id = page_id
        db.commit()
        return {"notion_page_id": page_id, "created": True}
    except APIResponseError as e:
        raise HTTPException(e.status, f"Notion error: {e}")


# ── Context helpers for other routes ──────────────────────────────────────────

@router.get("/context")
async def get_context_endpoint(db: Session = Depends(get_db)):
    return {"context": await fetch_notion_context(db)}


async def fetch_notion_context(db: Session) -> str:
    cfg = _get_config(db)
    if not cfg.api_token or not cfg.is_enabled:
        return ""
    page_ids = json.loads(cfg.context_page_ids or "[]")
    if not page_ids:
        return ""
    notion = _notion(cfg.api_token)
    parts = []
    for pid in page_ids[:5]:
        txt = await _read_page_text(notion, pid.strip(), max_chars=2000)
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


async def fetch_my_notes_for_app(app_notion_page_id: str, db: Session) -> str:
    cfg = _get_config(db)
    if not cfg.api_token or not app_notion_page_id:
        return ""
    try:
        notion = _notion(cfg.api_token)
        blocks = await _list_all_blocks(notion, app_notion_page_id)
        return _extract_section_text(blocks, _HEADING_MY_NOTES, end_at_next_h2=True)
    except APIResponseError as e:
        logger.warning(f"Could not read My Notes from {app_notion_page_id}: {e}")
        return ""


async def write_prep_to_notion(app_notion_page_id: str, prep: dict, db: Session) -> None:
    """Overwrite the AI prep section of an existing interview page."""
    cfg = _get_config(db)
    if not cfg.api_token or not app_notion_page_id:
        return
    try:
        notion = _notion(cfg.api_token)
        blocks = await _list_all_blocks(notion, app_notion_page_id)
        ai_ids = _find_section_block_ids(blocks, _HEADING_AI_PREP, _HEADING_MY_NOTES)
        for bid in ai_ids[1:]:
            try:
                await notion.blocks.delete(block_id=bid)
            except APIResponseError:
                pass
        new_blocks = _ai_prep_blocks(prep)[1:]
        if new_blocks:
            await notion.blocks.children.append(block_id=app_notion_page_id, children=new_blocks)
        logger.info(f"Updated AI prep in Notion page {app_notion_page_id}")
    except APIResponseError as e:
        logger.warning(f"Failed to write prep to Notion: {e}")


# ── Auto-create (background task) ─────────────────────────────────────────────

async def auto_create_interview_page(app_id: int) -> None:
    db = SessionLocal()
    try:
        from src.api.models import Application as App
        app = db.query(App).filter(App.id == app_id).first()
        if not app or app.notion_page_id:
            return
        cfg = _get_config(db)
        if not cfg.api_token or not cfg.is_enabled or not cfg.interviews_parent_page_id:
            return
        page_id = await _do_create_page(app, cfg, db)
        app.notion_page_id = page_id
        db.commit()
        logger.info(f"Auto-created Notion page for app {app_id}: {page_id}")
    except APIResponseError as e:
        logger.warning(f"Failed to auto-create Notion page for app {app_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error creating Notion page for app {app_id}: {e}")
    finally:
        db.close()


# ── AI prep blocks (used by write_prep_to_notion) ─────────────────────────────

def _ai_prep_blocks(prep: dict) -> list:
    sections = [
        ("❓ Likely Questions",     "likely_questions"),
        ("📚 Topics to Study",      "topics_to_study"),
        ("🧠 Behavioral Questions", "behavioral_questions"),
        ("🔍 Company Research",     "company_research"),
        ("💡 Tips",                 "tips"),
    ]
    blocks = [_h(2, _HEADING_AI_PREP)]
    for title, key in sections:
        items = prep.get(key, [])
        if items:
            blocks.append(_toggle(title, [_bullet(i) for i in items]))
    return blocks


# ── Low-level Notion helpers ───────────────────────────────────────────────────

async def _list_all_blocks(notion: AsyncClient, page_id: str) -> list:
    blocks = []
    try:
        resp = await notion.blocks.children.list(block_id=page_id, page_size=100)
        while True:
            blocks.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            resp = await notion.blocks.children.list(
                block_id=page_id, page_size=100, start_cursor=resp["next_cursor"]
            )
    except APIResponseError as e:
        logger.warning(f"Could not list blocks for {page_id}: {e}")
    return blocks


async def _read_page_text(notion: AsyncClient, page_id: str, max_chars: int = 3000) -> str:
    blocks = await _list_all_blocks(notion, page_id)
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        if btype in {"paragraph","heading_1","heading_2","heading_3",
                     "bulleted_list_item","numbered_list_item","to_do","quote","callout"}:
            rt = block.get(btype, {}).get("rich_text", [])
            txt = "".join(r.get("plain_text", "") for r in rt)
            if txt:
                lines.append(txt)
    return "\n".join(lines)[:max_chars]


def _find_section_block_ids(blocks: list, start: str, end: str) -> list[str]:
    ids, inside = [], False
    for block in blocks:
        btype = block.get("type", "")
        if btype in ("heading_1","heading_2","heading_3"):
            txt = "".join(r.get("plain_text","") for r in block.get(btype,{}).get("rich_text",[]))
            if txt == start:
                inside = True
            elif txt == end and inside:
                break
        if inside:
            ids.append(block["id"])
    return ids


def _extract_section_text(blocks: list, start: str, end_at_next_h2: bool = False) -> str:
    lines, inside = [], False
    for block in blocks:
        btype = block.get("type", "")
        if btype in ("heading_1","heading_2","heading_3"):
            txt = "".join(r.get("plain_text","") for r in block.get(btype,{}).get("rich_text",[]))
            if txt == start:
                inside = True
                continue
            elif inside and end_at_next_h2 and btype == "heading_2":
                break
        if inside and btype in {"paragraph","heading_3","bulleted_list_item",
                                  "numbered_list_item","to_do","quote","callout"}:
            rt = block.get(btype, {}).get("rich_text", [])
            txt = "".join(r.get("plain_text","") for r in rt)
            if txt:
                lines.append(txt)
    return "\n".join(lines)


def _extract_id(input_str: str) -> str:
    if not input_str:
        return ""
    import re
    m = re.search(r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})", input_str, re.I) \
        or re.search(r"([0-9a-f]{32})", input_str, re.I)
    return m.group(1) if m else input_str.strip()
