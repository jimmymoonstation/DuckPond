"""One-off/maintenance: re-run the email classifier against already-saved
'other'-category email_events so classifier improvements apply retroactively."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from src.email.reader import reprocess_stale_events

if __name__ == "__main__":
    print(reprocess_stale_events())
