# 🦆 DuckPond — Job Search Partner

An AI-powered job hunting system built for full-time job seekers on a deadline. It continuously scrapes every major ATS platform for fresh openings, tracks your application pipeline, syncs your inbox, and coaches you via Discord.

**Goal:** land a job within 2 months by staying organized, never missing a fresh opening, and having an AI partner that checks in on your progress daily.

---

## What it does

- **Scrapes 200+ companies** every 30 minutes across Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Amazon Jobs, and LinkedIn
- **LinkedIn poll every 5 minutes** — catches postings within the last 5 minutes in your target area
- **Browser extension** — analyze any job page for fit score + cover letter bullets, or quick-add it to the board
- **Gmail inbox sync every 15 minutes** — classifies emails (rejections, offers, interviews, LinkedIn DMs) and surfaces them in the dashboard
- **Discord coaching** — morning brief, evening check-in, daily report, plus conversational responses via Claude
- **Analytics** — applications/day line chart with 7-day rolling avg, status funnel, source breakdown, top companies
- **Learning pass** — reads your feedback on jobs and tunes the scraper's preferences over time

---

## Components

| Component | Purpose | Location |
|---|---|---|
| **Scraper** | ATS boards + Brave Search every 30 min | `src/scraper/` |
| **Email reader** | Gmail IMAP sync every 15 min | `src/email/` |
| **API** | FastAPI backend, all data access | `src/api/` |
| **Scheduler** | APScheduler (in-process) — 8 jobs | `src/api/scheduler.py` |
| **Dashboard** | Web UI — 7 tabs, sidebar layout | `src/dashboard/` |
| **Extension** | Chrome/Edge/Arc Manifest V3 extension | `extension/` |
| **Discord bot** | Conversational coaching + scheduled reports | `src/discord/` |
| **Database** | SQLite — 9 tables | `jobs.db` |

---

## Docs

| Doc | Contents |
|---|---|
| [Architecture & System Design](docs/architecture.md) | Full system diagram, component breakdown, data flows, deployment topology |
| [Database Schema](docs/schema.md) | All 9 tables with DDL, ERD, status state machine, index strategy |
| [API Specification](docs/api-spec.md) | Every endpoint with request/response shapes |
| [Scraper Design](docs/scraper.md) | ATS board clients, dedup strategy, validator |
| [Discord Bot](docs/discord-bot.md) | Notification schedule, conversational mode |
| [Deployment](docs/deployment.md) | nginx config, systemd services, DigitalOcean setup |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/jimmymoonstation/job-hunt-partner
cd job-hunt-partner
pip install -r requirements.txt

# 2. Environment
cp .env.example .env
# Fill in: BRAVE_API_KEY, DISCORD_BOT_TOKEN, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ANTHROPIC_API_KEY

# 3. Database
python scripts/init_db.py
python seed_companies.py   # seeds ~200 tracked companies

# 4. Run locally
uvicorn src.api.main:app --reload --port 5057

# 5. Deploy
bash scripts/deploy.sh
```

---

## Dashboard

Access at `http://<server>/jobs-dashboard`

Sidebar tabs (left nav):

| Tab | Description |
|---|---|
| 🦆 The Pond | Job board — all new openings. Glowing duck badge appears when new jobs arrive while you're away. |
| My Shots | Application tracker — sortable table by title, company, date applied, status |
| Mailbox | Email event feed — rejections, offers, interview invites, confirmations |
| Messages | LinkedIn DM inbox — messages parsed from Gmail notification emails |
| Analysis | Applications/day line chart, 7-day rolling avg, status funnel, source breakdown |
| Companies | Tracked company list + ATS portal (add by name, URL, or `greenhouse:stripe`) |
| Settings | Search config — titles, locations, levels, keywords, exclusions |

---

## Browser Extension

Load from `extension/` as an unpacked Manifest V3 extension in Chrome, Edge, or Arc.

1. Open any job posting page
2. Click the Duck Hunt icon
3. Hit **Analyze** — Claude reads the page and returns a fit score, strengths, gaps, and cover letter bullets
4. Hit **Save to Board** to add it to The Pond, or use **Quick Add** to skip analysis

Default server: `http://143.198.134.85` — configurable in extension settings.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy ORM, Pydantic v2 |
| Scheduler | APScheduler 3.x (AsyncIOScheduler, in-process) |
| Database | SQLite (JSON columns, single-writer, ~10k rows) |
| Scraper | httpx + BeautifulSoup4, Brave Search API |
| Email | imaplib (Gmail IMAP), rules-based classifier |
| AI | Anthropic Claude API (analysis, coaching, company discovery) |
| Dashboard | Vanilla HTML/CSS/JS, Chart.js 4.4.4 |
| Extension | Manifest V3, chrome.scripting, chrome.storage |
| Bot | discord.py |
| Server | nginx reverse proxy, systemd, DigitalOcean SFO3 |
