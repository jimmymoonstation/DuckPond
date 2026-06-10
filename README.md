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
| [Migration Plan](docs/migration.md) | Phased roadmap: Docker → Kafka → Flink → Trino → K8s, cost breakdown |

---

## Deploy on a new server

Fresh Ubuntu 22.04 or 24.04 server (root access required). Takes about 5 minutes.

```bash
git clone https://github.com/jimmymoonstation/DuckPond.git /opt/job-hunt-partner
bash /opt/job-hunt-partner/scripts/setup.sh
```

The script installs all dependencies, seeds 200+ companies, configures nginx and systemd, and prints exactly what to do next. After it finishes, two manual steps:

**1. Fill in your keys** (`nano /opt/job-hunt-partner/.env`):

| Key | Where to get it |
|---|---|
| `BRAVE_API_KEY` | [brave.com/search/api](https://brave.com/search/api/) — free, 2k req/month |
| `DISCORD_BOT_TOKEN` | [discord.com/developers/applications](https://discord.com/developers/applications) |
| `JOB_HUNT_CHANNEL_ID` | Right-click a channel in Discord → Copy Channel ID |
| `EMAIL_ADDRESS` | Your Gmail address |
| `EMAIL_APP_PASSWORD` | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2FA on) |

**2. Authenticate Claude Code** (powers the browser extension's Analyze button):
```bash
runuser -u claudebot -- claude auth login
# follow the browser link it prints, then sign into claude.ai
```

**Then start everything:**
```bash
systemctl start job-hunter
```

Dashboard is live at `http://<your-server-ip>/jobs-dashboard`.

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

### Current (Phase 0)

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

### Target (Phase 6+)

| Layer | Tech |
|---|---|
| Containers | Docker, k3s (Kubernetes) |
| Event bus | Redpanda (Kafka-compatible) |
| Stream processing | Apache Flink (PyFlink) |
| OLTP database | PostgreSQL |
| Object storage | MinIO (self-hosted S3) |
| Table format | Apache Iceberg |
| Analytics query layer | Trino |
| Scheduling | Kubernetes CronJobs (replaces APScheduler) |

All target-stack software is free and open-source. Only cost increase: DigitalOcean droplet upgrade ($6 → $48/mo for 8 GB RAM).

---

## Roadmap

| Phase | Description | Status |
|---|---|---|
| Phase 0 | Single-process systemd app, SQLite | **Current** |
| Phase 1 | Dockerize all services (Docker Compose) | Planned |
| Phase 2 | Add Redpanda (Kafka event bus) + droplet upgrade | Planned |
| Phase 3 | SQLite → PostgreSQL | Planned |
| Phase 4 | Add Apache Flink (stateful stream processing) | Planned |
| Phase 5 | Add MinIO + Apache Iceberg (data lake) | Planned |
| Phase 6 | Add Trino (federated analytics queries) | Planned |
| Phase 7 | docker-compose → k3s (Kubernetes) | Planned |

See [Migration Plan](docs/migration.md) for detailed steps, rollback strategy, and cost breakdown per phase.
