# Database Schema

> This document covers both the **current single-user schema** (SQLite) and the **target multi-user schema** (PostgreSQL). See [Migration Plan](migration.md) for how to get from one to the other.

---

## Target Schema — Multi-User (PostgreSQL)

### Design principles

- `jobs` is a **global catalog** — one row per unique job posting, shared across all users. Deduplication is global.
- `user_jobs` is the **per-user board** — a junction table linking users to jobs. All user-specific state (status, fit score, notes, feedback) lives here, not in `jobs`.
- Every other user-scoped table (`resumes`, `search_config`, `email_events`) gains a `user_id` FK.
- `tracked_companies` remains a global catalog (all users benefit from the same scraper targets). A `added_by_user_id` column tracks who added a company manually.

### Import flow

```
User clicks "Import" in extension
        │
POST /api/jobs  { company_job_id, title, company, url, source, ... }
Authorization: Bearer <user_api_key>
        │
FastAPI:
  Step 1 — UPSERT into global catalog:
    INSERT INTO jobs (...) ON CONFLICT (company_job_id, source) DO NOTHING
    → already exists globally? no-op. new job? insert it.

  Step 2 — Add to user's board:
    INSERT INTO user_jobs (user_id, job_id, status='saved')
    ON CONFLICT (user_id, job_id) DO NOTHING
    → user already has it? no-op.

  Step 3 — Return user_jobs row to extension
```

1000 users importing the same Stripe SWE role creates exactly **one row in `jobs`** and **one row per user in `user_jobs`**.

### Entity Relationship Diagram (target)

```mermaid
erDiagram
    users {
        UUID     id PK
        TEXT     email
        TEXT     name
        TEXT     api_key
        DATETIME created_at
        DATETIME last_active_at
    }

    jobs {
        UUID     id PK
        TEXT     company_job_id
        TEXT     company_name
        TEXT     job_title
        TEXT     location
        TEXT     level
        TEXT     url
        TEXT     original_url
        TEXT     source
        TEXT     description
        DATETIME posted_at
        DATETIME discovered_at
        BOOLEAN  is_active
    }

    user_jobs {
        UUID     id PK
        UUID     user_id FK
        UUID     job_id FK
        TEXT     status
        FLOAT    fit_score
        TEXT     cover_letter_bullets
        TEXT     user_feedback
        TEXT     notes
        DATETIME applied_at
        DATETIME added_at
    }

    status_history {
        UUID     id PK
        UUID     user_job_id FK
        TEXT     from_status
        TEXT     to_status
        DATETIME changed_at
        TEXT     notes
    }

    interviews {
        UUID     id PK
        UUID     user_job_id FK
        TEXT     round
        DATETIME scheduled_at
        TEXT     notes
        TEXT     outcome
        TEXT     prep_notes
    }

    resumes {
        UUID     id PK
        UUID     user_id FK
        TEXT     name
        TEXT     version
        TEXT     tags
        TEXT     content_json
        TEXT     file_path
        DATETIME created_at
    }

    search_config {
        UUID     id PK
        UUID     user_id FK
        TEXT     titles_json
        TEXT     locations_json
        TEXT     levels_json
        TEXT     keywords_json
        TEXT     excluded_companies_json
        BOOLEAN  is_active
        DATETIME updated_at
    }

    tracked_companies {
        UUID     id PK
        TEXT     company_name
        TEXT     ats_type
        TEXT     ats_slug
        TEXT     career_url
        UUID     added_by_user_id FK
        DATETIME added_at
        BOOLEAN  is_active
    }

    email_events {
        UUID     id PK
        UUID     user_id FK
        TEXT     message_id
        DATETIME received_at
        TEXT     from_address
        TEXT     category
        TEXT     company_name
        UUID     linked_user_job_id FK
        TEXT     snippet
        DATETIME processed_at
    }

    discord_sessions {
        UUID     id PK
        UUID     user_id FK
        TEXT     channel_id
        TEXT     message_history_json
        DATETIME last_active
    }

    users            ||--o{ user_jobs          : "board"
    jobs             ||--o{ user_jobs          : "imported by"
    user_jobs        ||--o{ status_history     : "audit trail"
    user_jobs        ||--o{ interviews         : "interview rounds"
    users            ||--o{ resumes            : "owns"
    users            ||--o{ search_config      : "owns"
    users            ||--o{ email_events       : "owns"
    users            ||--o{ discord_sessions   : "owns"
    user_jobs        ||--o{ email_events       : "linked email"
```

### Table Definitions (target — PostgreSQL)

#### `users`

```sql
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    api_key       TEXT NOT NULL UNIQUE DEFAULT gen_random_uuid()::TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ
);
```

The `api_key` is what the browser extension sends in the `Authorization` header. Simple and stateless — no session management needed.

---

#### `jobs` (global catalog)

```sql
CREATE TABLE jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_job_id  TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    job_title       TEXT NOT NULL,
    location        TEXT,
    level           TEXT,
    url             TEXT NOT NULL,
    original_url    TEXT,
    source          TEXT NOT NULL,
    description     TEXT,
    posted_at       TIMESTAMPTZ,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,

    UNIQUE(company_job_id, source)    -- global dedup constraint
);

CREATE INDEX idx_jobs_discovered  ON jobs(discovered_at DESC);
CREATE INDEX idx_jobs_company     ON jobs(company_name);
CREATE INDEX idx_jobs_active      ON jobs(is_active);
```

`user_feedback` and `fit_score` are **not** on this table — those are per-user and live in `user_jobs`.

---

#### `user_jobs` (per-user board)

```sql
CREATE TABLE user_jobs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id               UUID NOT NULL REFERENCES jobs(id),
    status               TEXT NOT NULL DEFAULT 'saved'
                             CHECK(status IN (
                                 'saved', 'applied', 'phone_screen',
                                 'interview', 'offer', 'rejected', 'withdrawn'
                             )),
    fit_score            FLOAT,             -- Claude fit score (0–100), NULL if not analyzed
    cover_letter_bullets TEXT,              -- JSON array of bullet strings from Claude
    user_feedback        TEXT,              -- free-text feedback for learning pass
    feedback_at          TIMESTAMPTZ,
    notes                TEXT,
    applied_at           TIMESTAMPTZ,       -- NULL when status='saved'
    added_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(user_id, job_id)                 -- one board entry per user per job
);

CREATE INDEX idx_user_jobs_user    ON user_jobs(user_id);
CREATE INDEX idx_user_jobs_status  ON user_jobs(user_id, status);
CREATE INDEX idx_user_jobs_added   ON user_jobs(user_id, added_at DESC);
```

---

#### `status_history`

```sql
CREATE TABLE status_history (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_job_id  UUID NOT NULL REFERENCES user_jobs(id) ON DELETE CASCADE,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes        TEXT
);
```

---

#### `interviews`

```sql
CREATE TABLE interviews (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_job_id  UUID NOT NULL REFERENCES user_jobs(id) ON DELETE CASCADE,
    round        TEXT NOT NULL
                     CHECK(round IN (
                         'phone_screen', 'technical', 'behavioral',
                         'system_design', 'take_home', 'final', 'other'
                     )),
    scheduled_at TIMESTAMPTZ,
    notes        TEXT,
    outcome      TEXT CHECK(outcome IN ('passed', 'failed', 'pending', 'cancelled')),
    prep_notes   TEXT
);
```

---

#### `resumes`

```sql
CREATE TABLE resumes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    version      TEXT,
    tags         TEXT DEFAULT '[]',
    content_json TEXT DEFAULT '{}',
    file_path    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

#### `search_config`

```sql
CREATE TABLE search_config (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    titles_json             TEXT NOT NULL DEFAULT '[]',
    locations_json          TEXT NOT NULL DEFAULT '[]',
    levels_json             TEXT NOT NULL DEFAULT '[]',
    keywords_json           TEXT NOT NULL DEFAULT '[]',
    excluded_companies_json TEXT NOT NULL DEFAULT '[]',
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

#### `tracked_companies` (global catalog)

```sql
CREATE TABLE tracked_companies (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name      TEXT NOT NULL,
    ats_type          TEXT NOT NULL,
    ats_slug          TEXT NOT NULL,
    workday_board     TEXT,
    workday_wd_ver    TEXT DEFAULT 'wd5',
    career_url        TEXT,
    discovered_from   TEXT NOT NULL DEFAULT 'manual',
    added_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    -- NULL = added by system (seed/auto-discovery)
    added_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active         BOOLEAN NOT NULL DEFAULT TRUE
);
```

---

#### `email_events`

```sql
CREATE TABLE email_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id          TEXT NOT NULL,
    received_at         TIMESTAMPTZ,
    from_address        TEXT,
    from_name           TEXT,
    subject             TEXT,
    category            TEXT NOT NULL DEFAULT 'other',
    company_name        TEXT,
    job_title           TEXT,
    linked_user_job_id  UUID REFERENCES user_jobs(id) ON DELETE SET NULL,
    snippet             TEXT,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(user_id, message_id)   -- dedup per user
);

CREATE INDEX idx_email_events_user     ON email_events(user_id, received_at DESC);
CREATE INDEX idx_email_events_category ON email_events(user_id, category);
```

---

#### `discord_sessions`

```sql
CREATE TABLE discord_sessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID REFERENCES users(id) ON DELETE CASCADE,
    channel_id           TEXT NOT NULL UNIQUE,
    message_history_json TEXT NOT NULL DEFAULT '[]',
    last_active          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

### What moved where

| Current column | Current table | Moved to |
|---|---|---|
| `user_feedback` | `jobs` | `user_jobs` |
| `feedback_at` | `jobs` | `user_jobs` |
| `fit_score` | nowhere (returned by API, not stored) | `user_jobs` |
| `cover_letter_bullets` | nowhere (returned by API, not stored) | `user_jobs` |
| `application_id` FK | `status_history`, `interviews`, `email_events` | `user_job_id` FK |
| `linked_application_id` | `email_events` | `linked_user_job_id` |

---

### Kafka event schema (multi-user)

All events flowing through Redpanda carry `user_id` so Flink can partition correctly:

```json
// raw_jobs topic — produced by scrapers (no user) and extension (with user)
{
  "user_id": "abc123",          // null for scraper-produced events
  "company_job_id": "gh:stripe:98765",
  "company_name": "Stripe",
  "job_title": "Senior Data Engineer",
  "source": "greenhouse",
  "url": "https://...",
  "discovered_at": "2026-06-07T10:00:00Z"
}

// user_job_events topic — produced when a user imports a job
{
  "user_id": "abc123",
  "job_id": "uuid-of-job-in-global-catalog",
  "action": "import",           // import | status_change | analyze
  "status": "saved",
  "fit_score": null,
  "timestamp": "2026-06-07T10:00:05Z"
}
```

Flink partitions `raw_jobs` by `company_job_id` (for global dedup) and `user_job_events` by `user_id` (for per-user ordering).

---

## Current Schema — Single User (SQLite)

> The schema below is the live schema as of Phase 0. It will be replaced by the target schema above during Phase 3 (SQLite → PostgreSQL) of the [migration plan](migration.md).

SQLite database at `/opt/job-hunt-partner/jobs.db`.

---

## Entity Relationship Diagram

```mermaid
erDiagram
    jobs {
        INTEGER id PK
        TEXT    company_job_id
        TEXT    company_name
        TEXT    job_title
        TEXT    location
        TEXT    level
        TEXT    url
        TEXT    original_url
        TEXT    source
        TEXT    description
        DATETIME posted_at
        DATETIME discovered_at
        BOOLEAN  is_active
        TEXT    user_feedback
        DATETIME feedback_at
    }

    applications {
        INTEGER  id PK
        INTEGER  job_id FK
        INTEGER  resume_id FK
        TEXT     status
        DATETIME applied_at
        DATETIME updated_at
        TEXT     notes
    }

    status_history {
        INTEGER  id PK
        INTEGER  application_id FK
        TEXT     from_status
        TEXT     to_status
        DATETIME changed_at
        TEXT     notes
    }

    interviews {
        INTEGER  id PK
        INTEGER  application_id FK
        TEXT     round
        DATETIME scheduled_at
        TEXT     notes
        TEXT     outcome
        TEXT     prep_notes
    }

    resumes {
        INTEGER  id PK
        TEXT     name
        TEXT     version
        TEXT     tags
        TEXT     content_json
        TEXT     file_path
        DATETIME created_at
    }

    search_config {
        INTEGER  id PK
        TEXT     titles_json
        TEXT     locations_json
        TEXT     levels_json
        TEXT     keywords_json
        TEXT     excluded_companies_json
        BOOLEAN  is_active
        DATETIME created_at
        DATETIME updated_at
    }

    tracked_companies {
        INTEGER  id PK
        TEXT     company_name
        TEXT     ats_type
        TEXT     ats_slug
        TEXT     workday_board
        TEXT     workday_wd_ver
        TEXT     career_url
        TEXT     discovered_from
        DATETIME added_at
        BOOLEAN  is_active
    }

    email_events {
        INTEGER  id PK
        TEXT     message_id
        DATETIME received_at
        TEXT     from_address
        TEXT     from_name
        TEXT     subject
        TEXT     category
        TEXT     company_name
        TEXT     job_title
        INTEGER  linked_application_id FK
        TEXT     snippet
        DATETIME processed_at
    }

    discord_sessions {
        INTEGER  id PK
        TEXT     channel_id
        TEXT     message_history_json
        DATETIME last_active
    }

    jobs         ||--o{ applications    : "applied to"
    applications ||--o{ status_history  : "audit trail"
    applications ||--o{ interviews      : "interview rounds"
    resumes      ||--o{ applications    : "used in"
    applications ||--o{ email_events    : "linked email"
```

---

## Table Definitions

### `jobs`
Master list of every job opening discovered by the scraper or added manually/via extension. Records are never deleted — `is_active` flips to `false` when the posting disappears.

```sql
CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_job_id  TEXT NOT NULL,      -- platform's own job ID (from URL or API)
    company_name    TEXT NOT NULL,
    job_title       TEXT NOT NULL,
    location        TEXT,
    level           TEXT,               -- "Senior", "L4", "New Grad", etc.
    url             TEXT NOT NULL,      -- source URL (LinkedIn, ATS board, etc.)
    original_url    TEXT,               -- company's own career page URL (when different)
    source          TEXT NOT NULL,      -- "greenhouse", "lever", "linkedin", "brave_search", "extension", etc.
    description     TEXT,
    posted_at       DATETIME,           -- company's posted date (NULL if not available)
    discovered_at   DATETIME NOT NULL DEFAULT (datetime('now')),
    is_active       BOOLEAN NOT NULL DEFAULT 1,
    user_feedback   TEXT,               -- free-text feedback for learning pass
    feedback_at     DATETIME,           -- when feedback was recorded
    UNIQUE(company_job_id, source)      -- deduplication constraint
);

CREATE INDEX idx_jobs_discovered ON jobs(discovered_at DESC);
CREATE INDEX idx_jobs_company    ON jobs(company_name);
CREATE INDEX idx_jobs_active     ON jobs(is_active);
```

**`source` values:** `greenhouse`, `lever`, `ashby`, `workday`, `smartrecruiters`, `amazon`, `linkedin`, `brave_search`, `extension`, `manual`

**`original_url` vs `url`:** `url` is always the page the scraper or extension found the job on. When the extension detects an ATS link embedded on a third-party page (e.g. LinkedIn → Greenhouse), `original_url` holds the ATS link so the dashboard can deep-link directly to the company's application form.

---

### `applications`
One row per job application. Status tracks the current stage in the hiring pipeline.

```sql
CREATE TABLE applications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    resume_id   INTEGER REFERENCES resumes(id),
    status      TEXT NOT NULL DEFAULT 'applied'
                    CHECK(status IN (
                        'saved',        -- bookmarked, not yet applied
                        'applied',      -- submitted
                        'phone_screen', -- recruiter call
                        'interview',    -- technical / behavioral rounds
                        'offer',        -- received an offer
                        'rejected',     -- rejected at any stage
                        'withdrawn'     -- candidate withdrew
                    )),
    applied_at  DATETIME,               -- NULL when status='saved'
    updated_at  DATETIME NOT NULL DEFAULT (datetime('now')),
    notes       TEXT
);

CREATE INDEX idx_applications_status ON applications(status);
CREATE INDEX idx_applications_job    ON applications(job_id);
```

---

### `status_history`
Append-only audit log of every status transition. Written automatically by the API on every `PATCH /applications/{id}`.

```sql
CREATE TABLE status_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    from_status     TEXT,               -- NULL for the initial entry
    to_status       TEXT NOT NULL,
    changed_at      DATETIME NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);
```

---

### `interviews`
One row per interview round. An application can have many rounds.

```sql
CREATE TABLE interviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    round           TEXT NOT NULL
                        CHECK(round IN (
                            'phone_screen', 'technical', 'behavioral',
                            'system_design', 'take_home', 'final', 'other'
                        )),
    scheduled_at    DATETIME,
    notes           TEXT,               -- notes during or after the interview
    outcome         TEXT
                        CHECK(outcome IN ('passed', 'failed', 'pending', 'cancelled')),
    prep_notes      TEXT                -- Claude-generated prep material
);
```

---

### `resumes`
Flexible resume storage. `content_json` stores structured resume data; `file_path` optionally points to the raw PDF/DOCX stored in `uploads/`.

```sql
CREATE TABLE resumes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,         -- e.g. "Data Engineering v3"
    version      TEXT,                  -- semver or free-form label
    tags         TEXT DEFAULT '[]',     -- JSON array: ["python","dbt","senior"]
    content_json TEXT DEFAULT '{}',     -- full resume as JSON (see structure below)
    file_path    TEXT,                  -- /opt/job-hunt-partner/uploads/resume-v3.pdf
    created_at   DATETIME NOT NULL DEFAULT (datetime('now'))
);
```

**`content_json` document structure:**
```json
{
  "summary": "...",
  "experience": [
    {
      "company": "Acme Corp",
      "title": "Senior Data Engineer",
      "start": "2022-01",
      "end": "2024-12",
      "bullets": ["Built X that did Y", "..."]
    }
  ],
  "education": [{ "school": "...", "degree": "...", "year": 2020 }],
  "skills": ["Python", "dbt", "Spark", "Airflow"],
  "links": {
    "github":   "https://github.com/...",
    "linkedin": "https://linkedin.com/in/..."
  }
}
```

---

### `search_config`
User's job search preferences. The scraper reads the row where `is_active=1` on every run. Only one active row at a time.

```sql
CREATE TABLE search_config (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    titles_json             TEXT NOT NULL DEFAULT '[]',
    -- ["Data Engineer", "Analytics Engineer", "ML Engineer"]
    locations_json          TEXT NOT NULL DEFAULT '[]',
    -- ["San Francisco", "Remote", "New York"]
    levels_json             TEXT NOT NULL DEFAULT '[]',
    -- ["Senior", "Staff", "L5"]
    keywords_json           TEXT NOT NULL DEFAULT '[]',
    -- ["dbt", "Spark", "Python"]
    excluded_companies_json TEXT NOT NULL DEFAULT '[]',
    -- companies to skip entirely
    is_active               BOOLEAN NOT NULL DEFAULT 1,
    created_at              DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at              DATETIME NOT NULL DEFAULT (datetime('now'))
);
```

---

### `tracked_companies`
Master list of companies the scraper polls on every career-page cycle. Populated via the Companies tab portal, seed script, or automated discovery.

```sql
CREATE TABLE tracked_companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name    TEXT NOT NULL,
    ats_type        TEXT NOT NULL,
    -- greenhouse | lever | ashby | workday | smartrecruiters | amazon | custom
    ats_slug        TEXT NOT NULL,
    -- ATS-specific tenant identifier (e.g. "stripe" for Greenhouse, "stripe" for Lever)
    workday_board   TEXT,
    -- Workday board path, e.g. "Cisco_Careers" (Workday only)
    workday_wd_ver  TEXT DEFAULT 'wd5',
    -- Workday data-center version: wd1 | wd5 | wd12 (Workday only)
    career_url      TEXT,
    -- canonical career homepage URL for reference
    discovered_from TEXT NOT NULL DEFAULT 'manual',
    -- manual | seed | auto (auto = found by company_discovery job)
    added_at        DATETIME NOT NULL DEFAULT (datetime('now')),
    is_active       BOOLEAN NOT NULL DEFAULT 1
);
```

**ATS slug examples:**

| ATS | Company | `ats_slug` | `workday_board` |
|---|---|---|---|
| Greenhouse | Stripe | `stripe` | — |
| Lever | Figma | `figma` | — |
| Ashby | Linear | `linear` | — |
| Workday | Cisco | `cisco` | `Cisco_Careers` |
| Workday | Microsoft | `microsoft` | `microsoftcareers` |
| SmartRecruiters | KPMG | `KPMG` | — |
| Amazon | Amazon | `amazon` | — |

---

### `email_events`
Every email processed by the Gmail IMAP reader. Deduplicated by `message_id`. Used to power the Mailbox tab and Messages tab (LinkedIn DMs).

```sql
CREATE TABLE email_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id              TEXT UNIQUE NOT NULL,   -- RFC 2822 Message-ID header
    received_at             DATETIME,
    from_address            TEXT,
    from_name               TEXT,
    subject                 TEXT,
    category                TEXT NOT NULL DEFAULT 'other',
    -- offer | interview | assessment | rejection |
    -- application_confirm | linkedin_message | other
    company_name            TEXT,
    -- for linkedin_message: stores the sender's name (from subject parsing)
    job_title               TEXT,
    linked_application_id   INTEGER REFERENCES applications(id),
    -- matched to an application by company name (fuzzy)
    snippet                 TEXT,
    -- for linkedin_message: cleaned message preview (first ~250 chars)
    -- for others: first 250 chars of email body
    processed_at            DATETIME NOT NULL
);

CREATE INDEX idx_email_events_received  ON email_events(received_at DESC);
CREATE INDEX idx_email_events_category  ON email_events(category);
```

**LinkedIn DM storage note:** Because `email_events` was designed for job emails, LinkedIn DMs reuse existing columns: `company_name` stores the sender's display name (parsed from the subject line), and `snippet` stores the message preview (first paragraph of the email body, footer stripped).

---

### `discord_sessions`
Per-channel conversation history for the Discord bot. Kept to the last 20 messages to control Claude API token usage.

```sql
CREATE TABLE discord_sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id           TEXT NOT NULL UNIQUE,
    message_history_json TEXT NOT NULL DEFAULT '[]',
    -- [{role: "user"|"assistant", content: "...", timestamp: "..."}]
    -- trimmed to last 20 entries on each write
    last_active          DATETIME NOT NULL DEFAULT (datetime('now'))
);
```

---

## Application Status State Machine

```
              ┌─────────┐
              │  saved  │ ◄── bookmarked from board
              └────┬────┘
                   │ user clicks Apply
                   ▼
              ┌─────────┐
              │ applied │
              └────┬────┘
                   │ recruiter contacts
                   ▼
           ┌───────────────┐
           │  phone_screen │
           └───────┬───────┘
                   │ passes screen
                   ▼
           ┌───────────────┐
           │   interview   │ ◄── multiple rounds tracked in interviews table
           └───────┬───────┘
                   │
         ┌─────────┴─────────┐
         ▼                   ▼
     ┌───────┐          ┌──────────┐
     │ offer │          │ rejected │
     └───────┘          └──────────┘

  withdrawn ◄── valid exit from any state except saved
```

Every transition is recorded in `status_history` with a timestamp and optional notes.

---

## Key Indexes & Query Patterns

| Query | Index / Constraint |
|---|---|
| New jobs since last visit (🦆 badge) | `idx_jobs_discovered` |
| Jobs by company (dedup) | `UNIQUE(company_job_id, source)` |
| Active applications by status | `idx_applications_status` |
| Application history for a job | `idx_applications_job` |
| Email feed by date | `idx_email_events_received` |
| LinkedIn DMs filter | `idx_email_events_category` |
| Analysis funnel query | Full scan on `applications` (small table, fine) |
