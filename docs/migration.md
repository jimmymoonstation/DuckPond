# Migration Plan: SQLite → Cloud-Native Data Stack

This document tracks the planned evolution of DuckPond from a single-process systemd app to a fully containerized, event-driven data platform. Each phase is independently deployable and leaves the system in a working state.

---

## Cost Summary

All software in this migration is **free and open-source**. The only real cost is compute.

| Phase | Droplet | Monthly Cost | Notes |
|---|---|---|---|
| Current (Phase 0) | 1 vCPU / 1 GB | ~$6 | Too small for phases 2+ |
| Phase 1 (Docker Compose) | 1 vCPU / 1 GB | ~$6 | No RAM increase needed |
| Phase 2+ (Kafka + Flink + Trino) | 4 vCPU / 8 GB | ~$48 | Upgrade before Phase 2 |

**Upgrade the droplet before starting Phase 2.** Resize via DigitalOcean dashboard (live resize, no data loss).

No managed services required. Everything runs self-hosted in containers:
- Redpanda (Kafka-compatible broker) — free
- Apache Flink — free
- PostgreSQL — free
- MinIO (self-hosted S3) — free
- Apache Iceberg — free
- Trino — free
- k3s (lightweight Kubernetes) — free

---

## Before: Current Architecture (Phase 0)

```
Internet (job boards, Gmail)
         │
         ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Single DigitalOcean Droplet (1 vCPU / 1 GB RAM / $6)  │
  │                                                         │
  │  systemd: job-hunter.service                            │
  │  ├── FastAPI (uvicorn :5057)                            │
  │  │   └── APScheduler (in-process threads)              │
  │  │       ├── Scraper (every 30 min)                     │
  │  │       ├── Email reader (every 15 min)                │
  │  │       ├── LinkedIn poll (every 5 min)                │
  │  │       └── Learning pass (every 60 min)               │
  │  └── SQLite (jobs.db — single file, single writer)      │
  │                                                         │
  │  systemd: claude-discord-bot.service                    │
  │  └── Discord bot (discord.py)                           │
  │                                                         │
  │  nginx (reverse proxy → :5057, static dashboard)        │
  └─────────────────────────────────────────────────────────┘
```

**What works well:**
- Zero infrastructure overhead
- Simple to deploy and debug
- Fast iteration

**Pain points at scale:**
- APScheduler shares memory with the API — a scraper crash takes down the API
- SQLite single-writer blocks concurrent scraper runs
- No replay: if the scraper crashes mid-run, events are lost
- No separation between ingestion, processing, and serving layers

---

## After: Target Architecture (Phase 6)

```
Internet (job boards, Gmail, LinkedIn)
         │
         ▼
┌────────────────────────────────────────────────────────────────────┐
│  k3s Kubernetes Cluster (4 vCPU / 8 GB RAM / $48/mo)              │
│                                                                    │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐  │
│  │ scraper-job     │  │ email-reader-job │  │ linkedin-job     │  │
│  │ (K8s CronJob)   │  │ (K8s CronJob)   │  │ (K8s CronJob)    │  │
│  │ every 30 min    │  │ every 15 min    │  │ every 5 min      │  │
│  └────────┬────────┘  └────────┬────────┘  └────────┬─────────┘  │
│           │                   │                     │             │
│           └───────────────────┴─────────────────────┘             │
│                               │ produce events                    │
│                               ▼                                   │
│                   ┌───────────────────────┐                       │
│                   │  Redpanda (Kafka API) │                       │
│                   │  Topics:              │                       │
│                   │  • raw_jobs           │                       │
│                   │  • raw_emails         │                       │
│                   │  • job_events         │                       │
│                   │  • email_events       │                       │
│                   └───────────┬───────────┘                       │
│                               │ consume + process                 │
│                               ▼                                   │
│                   ┌───────────────────────┐                       │
│                   │  Apache Flink         │                       │
│                   │  Jobs:                │                       │
│                   │  • DeduplicateJobs    │                       │
│                   │    (keyed by          │                       │
│                   │     company_job_id,   │                       │
│                   │     30-min window)    │                       │
│                   │  • ClassifyEmails     │                       │
│                   │  • NormalizeJobs      │                       │
│                   └──────┬──────────┬─────┘                       │
│                          │          │                              │
│                    OLTP  │          │ analytics sink              │
│                          ▼          ▼                              │
│             ┌─────────────┐  ┌───────────────┐                   │
│             │ PostgreSQL  │  │ MinIO + Iceberg│                   │
│             │ (OLTP)      │  │ (data lake)   │                   │
│             │ jobs        │  │ jobs_history/ │                   │
│             │ applications│  │ emails/       │                   │
│             │ email_events│  │ applications/ │                   │
│             │ interviews  │  │ (Parquet)     │                   │
│             └──────┬──────┘  └──────┬────────┘                   │
│                    │                │                              │
│                    ▼                ▼                              │
│             ┌─────────────────────────────┐                       │
│             │  Trino (query layer)        │                       │
│             │  • federated SQL over PG    │                       │
│             │    + Iceberg                │                       │
│             │  • analytical queries       │                       │
│             │  • dashboard stats          │                       │
│             └──────────────┬─────────────┘                        │
│                            │                                      │
│             ┌──────────────┼──────────────┐                       │
│             ▼              ▼              ▼                        │
│      ┌───────────┐  ┌──────────┐  ┌─────────────┐                │
│      │ FastAPI   │  │ Discord  │  │ nginx ingress│                │
│      │ Deployment│  │ Bot      │  │ → dashboard  │                │
│      │ (2 pods)  │  │ Deployment│  │   + /api    │                │
│      └───────────┘  └──────────┘  └─────────────┘                │
└────────────────────────────────────────────────────────────────────┘
```

---

## Migration Phases

### Phase 1 — Dockerize Everything (no behavior change)

**Goal:** Wrap each service in a container. System behavior is identical to Phase 0.

**What changes:**
- Add `Dockerfile` per service (api, discord-bot, scraper)
- Add `docker-compose.yml` — all services + nginx
- Replace systemd service management with `docker compose up -d`
- Persist `jobs.db` via Docker volume

**What stays the same:**
- SQLite as the database
- APScheduler inside the API process
- All scraper logic unchanged

**docker-compose services:**
```yaml
services:
  api:         # FastAPI + APScheduler
  discord-bot: # discord.py bot
  nginx:       # reverse proxy
volumes:
  db-data:     # mounts jobs.db
```

**Cost:** No change ($6/mo — runs on existing droplet)

---

### Phase 2 — Add Redpanda (Kafka) + Upgrade Droplet

**Goal:** Introduce the event bus. Scrapers write to Kafka topics instead of directly to SQLite. A simple Kafka consumer (Python) reads events and writes to SQLite. System behavior is unchanged; we've just inserted a durable queue in the middle.

**Upgrade the droplet first:** 4 vCPU / 8 GB / $48/mo

**What changes:**
- Add `redpanda` container to docker-compose
- Scraper publishes `raw_job` events to topic `raw_jobs` instead of writing to DB
- New `consumer` service: reads `raw_jobs` → dedup → INSERT to SQLite
- Email reader publishes to `raw_emails` topic
- New consumer reads `raw_emails` → classify → INSERT to SQLite

**Topics:**
```
raw_jobs     ← scrapers produce here (raw, unnormalized)
raw_emails   ← email reader produces here
job_events   ← normalized jobs (consumer publishes after dedup)
email_events ← classified emails (consumer publishes after classify)
```

**Why this before Flink:** Kafka decouples producers from consumers now. If the scraper crashes, events are buffered and replayed when it recovers. The consumer is still simple Python — no Flink yet.

**Cost:** $48/mo (droplet upgrade is the only change)

---

### Phase 3 — Replace SQLite with PostgreSQL

**Goal:** Move from SQLite single-writer to PostgreSQL, which supports concurrent writes and is production-grade.

**What changes:**
- Add `postgres` container to docker-compose
- Migrate schema from SQLite DDL to PostgreSQL DDL
- Update `DATABASE_URL` env var from `sqlite:///` to `postgresql://`
- SQLAlchemy handles the rest (minimal code changes)
- Run `init_db.py` against PostgreSQL
- One-time data migration: `sqlite3 jobs.db .dump | psql`

**Why now (before Flink):** PostgreSQL is the permanent OLTP store. Flink will write to it in Phase 4 — better to have it running and validated first.

**Cost:** No change ($48/mo — PostgreSQL runs on same droplet)

---

### Phase 4 — Add Apache Flink (replace Python consumers)

**Goal:** Replace the simple Python Kafka consumers from Phase 2 with Flink jobs. This gives us stateful stream processing: proper keyed deduplication, windowed aggregations, and fault-tolerant exactly-once semantics.

**What changes:**
- Add `flink-jobmanager` and `flink-taskmanager` containers
- Write Flink jobs (Python or Java):
  - `DeduplicateJobsJob`: keyed by `company_job_id`, 30-min dedup window → writes to PostgreSQL `jobs`
  - `ClassifyEmailsJob`: classifies raw emails → writes to PostgreSQL `email_events`
  - `NormalizeJobsJob`: extracts title/company/level/location → enriches job records
- Remove Python consumer services from Phase 2

**Flink state backend:** RocksDB (persisted to MinIO in Phase 5 for checkpointing)

**Why Flink over Kafka Streams:** Flink has better support for complex windowed deduplication and is more transferable as a skill. Kafka Streams is tied to JVM — Flink has a Python API (PyFlink).

**Cost:** No change ($48/mo — Flink runs on same droplet)

---

### Phase 5 — Add MinIO + Iceberg (data lake)

**Goal:** Add an analytics storage layer. Flink sinks a copy of processed events to Iceberg tables (Parquet files on MinIO). This creates a queryable history of everything — jobs seen, emails received, application state over time.

**What changes:**
- Add `minio` container to docker-compose
- Configure Flink to write Iceberg sinks in addition to PostgreSQL:
  - `jobs_history` Iceberg table
  - `email_history` Iceberg table
  - `application_snapshots` Iceberg table
- MinIO stores Parquet files at `s3://duckpond/warehouse/`
- Flink checkpoints also go to MinIO (`s3://duckpond/checkpoints/`)

**Iceberg gives you:**
- Schema evolution (add columns without rewriting data)
- Time travel (query the table as it looked on any past date)
- Compaction (merge small Parquet files into large ones)

**Cost:** No change ($48/mo — MinIO runs on same droplet)

---

### Phase 6 — Add Trino (analytics query layer)

**Goal:** Add Trino as a federated SQL engine that can query both PostgreSQL (live OLTP data) and Iceberg (historical lake data) in a single SQL query.

**What changes:**
- Add `trino` container to docker-compose
- Configure Trino catalogs:
  - `postgresql` catalog → connects to PostgreSQL
  - `iceberg` catalog → connects to MinIO/Iceberg warehouse
- Update dashboard analytics endpoints to use Trino for historical queries
- OLTP writes (job inserts, application updates) still go directly to PostgreSQL

**Example Trino query:**
```sql
-- Cross-catalog: join live applications with historical job data
SELECT
    a.company,
    COUNT(*) AS applications,
    AVG(j.fit_score) AS avg_fit_score,
    MIN(j.discovered_at) AS earliest_posting
FROM postgresql.public.applications a
JOIN iceberg.duckpond.jobs_history j
    ON a.job_id = j.id
WHERE j.discovered_at >= CURRENT_DATE - INTERVAL '30' DAY
GROUP BY a.company
ORDER BY applications DESC
```

**When Trino is overkill (and you can skip it):** If you only need to query PostgreSQL, PostgreSQL's own analytics functions (window functions, CTEs, GROUPING SETS) are sufficient. Add Trino when you want federated queries across the lake + live DB, or when you want to practice with a data warehouse query engine.

**Cost:** No change ($48/mo — Trino runs on same droplet)

---

### Phase 7 — Move to Kubernetes (k3s)

**Goal:** Replace docker-compose with k3s (lightweight single-node Kubernetes). Services become Deployments, scrapers become CronJobs, config becomes ConfigMaps/Secrets.

**What changes:**
- Install k3s on the droplet (`curl -sfL https://get.k3s.io | sh -`)
- Convert docker-compose services to Kubernetes manifests:
  - `Deployment` for API, Discord bot, Flink, Trino, Redpanda, PostgreSQL, MinIO
  - `CronJob` for scraper (every 30 min), email reader (every 15 min), LinkedIn poll (every 5 min)
  - `Service` + nginx `Ingress` for HTTP routing
  - `PersistentVolumeClaim` for PostgreSQL and MinIO data
  - `Secret` for all env vars (ANTHROPIC_API_KEY, etc.)
- APScheduler is fully removed — Kubernetes CronJobs handle all scheduling

**k3s vs full K8s:**
k3s is production-grade Kubernetes packaged as a single ~70 MB binary. It runs the full K8s API (kubectl, Helm, ingress controllers all work) but without the overhead of kubeadm. Perfect for single-node or small clusters.

**Cost:** No change ($48/mo — k3s is free, runs on same droplet)

---

## Phase Summary

| Phase | Key Change | DB | Scheduler | Cost/mo |
|---|---|---|---|---|
| 0 (now) | systemd, in-process | SQLite | APScheduler | $6 |
| 1 | Docker Compose | SQLite | APScheduler | $6 |
| 2 | + Redpanda (Kafka) | SQLite | APScheduler | $48 * |
| 3 | SQLite → PostgreSQL | PostgreSQL | APScheduler | $48 |
| 4 | + Flink (replace consumers) | PostgreSQL | APScheduler | $48 |
| 5 | + MinIO + Iceberg | PostgreSQL + Iceberg | APScheduler | $48 |
| 6 | + Trino | PostgreSQL + Iceberg | APScheduler | $48 |
| 7 | docker-compose → k3s | PostgreSQL + Iceberg | K8s CronJobs | $48 |

\* Droplet upgrade from $6 → $48 happens at Phase 2.

---

## Rollback Strategy Per Phase

Every phase leaves the system in a fully working state. Rollback is always:

```bash
# Phase 1: revert to systemd
docker compose down
systemctl start job-hunter

# Phase 2: drain Kafka, disable producers, re-enable direct DB writes
# Phase 3: restore SQLite from backup before postgres migration
# Phase 4: re-deploy Python consumers, stop Flink jobs
# Phase 5: MinIO is additive — just stop writing to Iceberg sinks
# Phase 6: remove Trino, repoint analytics endpoints to PostgreSQL
# Phase 7: run docker compose up -d, uninstall k3s
```

---

## New Service Port Map (Phase 7)

| Service | Internal Port | Exposed |
|---|---|---|
| FastAPI | 5057 | via nginx ingress |
| PostgreSQL | 5432 | internal only |
| Redpanda (Kafka) | 9092 | internal only |
| Redpanda Console | 8080 | optional dev access |
| Flink Web UI | 8081 | optional dev access |
| MinIO API | 9000 | internal only |
| MinIO Console | 9001 | optional dev access |
| Trino | 8082 | internal only |
| nginx ingress | 80/443 | public |
