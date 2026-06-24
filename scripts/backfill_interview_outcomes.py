"""One-off: apply _resolve_interview_outcomes retroactively against every
already-classified email_event (interview/rejection/offer), in received_at
order per application, so existing interview rows get a real outcome instead
of staying permanently 'Pending'."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import text
from src.api.database import SessionLocal
from src.api.models import Application
from src.email.reader import _resolve_interview_outcomes

if __name__ == "__main__":
    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT linked_application_id, category, received_at
            FROM email_events
            WHERE category IN ('interview', 'rejection', 'offer')
            AND linked_application_id IS NOT NULL
            ORDER BY linked_application_id, received_at ASC
        """)).fetchall()

        total = 0
        for app_id, category, _received_at in rows:
            app = db.query(Application).filter(Application.id == app_id).first()
            if not app:
                continue
            total += _resolve_interview_outcomes(db, app, category)
        db.commit()
        print(f"Backfilled {total} interview outcome(s) across {len(rows)} email events")
