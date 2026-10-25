# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Source of truth for the narrative below: `MYC_ATS_Technical_Architecture.docx` (accurate, validated against 269 real candidate resumes), reconciled with the current codebase after the production-hardening pass described in Task 8's commit history.

## Project Overview

Marine ATS — a resume scoring and rewriting service for Indian seafarer resumes, embedded in Marine Your Career's app via REST endpoints — submit for the full scored + AI-rewritten result, or score for a fast, Groq-free scores-only check (see "API reference" for the complete, current list; plus operational/admin/health endpoints).

Indian seafarer resumes are functionally regulatory compliance documents, not marketing documents. Recruiters scan for three things in strict priority order: valid certificates (eligibility gate), unbroken sea service records (verification layer), and vessel type experience (differentiation layer). Most candidates submit Canva-template resumes with multi-column layouts, scattered certification mentions, and no ATS awareness — these get auto-rejected by manning agency screening software before a human ever reads them.

The system ingests a raw resume PDF or DOCX, scores it against both general ATS rules and maritime-specific domain rules, rewrites it using an LLM, and outputs a clean, ATS-compliant DOCX — with a measurable before/after score on both scales.

**Validation**: 269 real candidate resumes processed end-to-end, zero pipeline errors. Avg ATS score 63.4→69.1/100, avg maritime score 54.1→70.7 (on the /65 base + /20 bonus scale), resumes with 0 sea-months detected dropped from 56 to 2, ATS FAIL grade count dropped from 82 to 1.

## Architecture

A modular monolith: FastAPI + SQLite (WAL) + `BackgroundTasks` + Groq, single VPS deployment, single client. PostgreSQL/Celery/Redis/multi-tenancy are deliberately deferred, not oversights — see "Explicitly out of scope" below for the specific trigger conditions that would justify revisiting each one; none has been hit.

```mermaid
flowchart TD
    U[Resume PDF/DOCX] --> S1[1: Extract\nextractor.py]
    S1 --> S2[2: Parse\nparser.py]
    S2 --> S3[3: ATS Score before\nscorer.py]
    S2 --> S4[4: Maritime Score before\nmaritime.py]
    S3 --> CP[CandidateProfile\nmodels/candidate_profile.py]
    S4 --> CP
    CP --> S5[5: AI Rewrite\nservices/ai_client.py — Groq qwen/qwen3.6-27b]
    S5 --> S6[6: Generate DOCX\ngenerator.py]
    S6 --> S7[7: Re-score\nscorer.py + maritime.py again]
    S7 --> OUT[ATS-optimised DOCX +\nbefore/after scores]

    subgraph rules[rules/ — externalized data]
        CR[cert_rules.py]
        AL[aliases.py]
    end
    S4 -.reads.-> rules
    S2 -.reads.-> rules
```

Only Stage 5 (AI Rewrite) is non-deterministic. Everything else is pure rules and math — every point gained or lost traces to a named rule (`[R1]`–`[R42]`, see issue strings throughout `scorer.py`/`maritime.py`), not a black box.

### Pipeline stages (in `pipeline/`)

| # | Stage | File | What it does | Tech |
|---|---|---|---|---|
| 1 | Extract | `pipeline/extractor.py` | Reads PDF/DOCX, detects two-column layouts, reconstructs reading order using word x/y coordinates. Flags structural ATS violations (tables, headers/footers, images, scanned PDFs). | pdfplumber, python-docx |
| 2 | Parse | `pipeline/parser.py` | Extracts name, contact, sections, skills, rank. Pulls sea-service months from PDF service-record tables (`extract_sea_months`), with a text-based fallback (`extract_sea_months_from_text`) for when there's no PDF — e.g. re-scoring a generated DOCX. | spaCy (`en_core_web_lg`), regex, `python-dateutil` |
| 3 | ATS Score | `pipeline/scorer.py` | Format (40 pts) + Sections (20 pts) + Keywords (40 pts, blended TF-IDF + raw coverage). | scikit-learn TF-IDF, cosine similarity |
| 4 | Maritime Score | `pipeline/maritime.py` | Domain rules: cert requirements per rank, sea-service minimums, vessel-type match, bonus differentiation tier (0-20 pts). Rule tables live in `rules/`, not here. | Custom rule engine over `rules/cert_rules.py`, `rules/aliases.py` |
| 5 | AI Rewrite | `services/ai_client.py` | LLM rewrites summary, bullets, skills, certs to ATS spec. Raw-text fallback for garbled resumes. Retries transient Groq failures (timeout/rate-limit/connection) with exponential backoff (1s/2s/4s). Output validated, never trusted blindly. | Groq API (`qwen/qwen3.6-27b`) |
| 6 | Generate | `pipeline/generator.py` | Builds a clean single-column DOCX (Arial, no tables, no images, no text boxes). | python-docx |
| 7 | Re-score | `pipeline/scorer.py` + `pipeline/maritime.py` again | Re-runs both scorers on the generated DOCX to confirm the rewrite actually improved things — this is the loop that the maritime re-score bug (see "Known-fixed bugs" below) broke before it was fixed. | — |

**Entry points:**
- `main.py` — CLI, single resume: `venv/bin/python main.py <resume_path> [job_description]`. Still the manual single-resume testing path; every change in this repo must keep it working.
- `api.py` — FastAPI service, the production path MYC's app calls.
- `pipeline/batch_processor.py` — batch-processes a list of local resumes (`data/resume_file_list.txt`) for regression testing against the 269-resume validation set. Outputs `data/outputs/batch_results.csv` / `.json`.
- `training/prelabel.py` — offline pre-labeling tool (Ollama/qwen2.5:7b) used to build training data for a possible future custom NER model (see Roadmap). Not part of the live pipeline.

### Folder structure

```
.
├── api.py                    # FastAPI service — the REST endpoints MYC's app calls (see "API reference")
├── main.py                   # CLI entry point for manual single-resume testing
├── config.py                 # API keys, model names, scoring weights/thresholds — single source
├── pipeline/                 # Stage-execution layer — extract, parse, score, maritime, generate, batch
│   ├── extractor.py
│   ├── parser.py
│   ├── scorer.py
│   ├── maritime.py
│   ├── generator.py
│   └── batch_processor.py
├── models/
│   └── candidate_profile.py  # CandidateProfile — the typed domain object (see below)
├── services/
│   └── ai_client.py          # Sole Groq import site for the rewrite pipeline (health check pings Groq separately — see below)
├── rules/                    # Externalized maritime rule tables (data, not logic)
│   ├── cert_rules.py         # CERT_REQUIREMENTS, SEA_SERVICE_MIN
│   └── aliases.py            # RANK_KEYWORDS, CERT_ALIASES
├── tests/                    # pytest — scorer.py and maritime.py coverage
├── training/
│   └── prelabel.py           # Offline NER training-data prep, not part of the live pipeline
├── data/                     # gitignored — test_resumes/, outputs/, jobs.db, maritime_skills.json
├── deploy.sh                 # Production deploy script (see "Deployment" below)
├── env_template.txt          # Copy to .env and fill in — see "Environment variables" below
├── crontab_example.txt       # Nightly /ats/cleanup cron entry
└── requirements.txt
```

`pipeline/` stays the stage-execution layer; it does not import from `models/`, `services/`, or `rules/` beyond what the rule tables and generic types require. New pipeline-adjacent logic goes in `models/`, `services/`, or `rules/`, not into `pipeline/` directly — this is a deliberate, settled decision, not an oversight (see the architecture doc's "no further layout debate" framing).

### Domain model: `CandidateProfile`

`models/candidate_profile.py` defines `CandidateProfile` (Pydantic) — the canonical typed object for a candidate, built once per job right after Stage 4 (maritime scoring) via `build_candidate_profile(parsed, maritime_result)`:

```python
class CandidateProfile(BaseModel):
    name: str
    contact: ContactInfo
    rank_detected: str
    license_rank: str
    sea_months: int
    certificates: list[str]
    missing_certs: list[str]
    vessel_types: list[str]
    skills: list[str]
    sections: dict[str, str]
```

Stage 5 (rewrite) and the name/contact injection before Stage 6 (generate) read `rank_detected`, `license_rank`, `name`, and `contact` from this object rather than re-deriving them from the loose `parsed`/`maritime` dicts. This is the boundary where the maritime re-score bug (below) lived — Pydantic validation at construction is a write-time check against malformed upstream data, not just something caught by manual score audits.

It is **not** force-fit everywhere: `rewrite_resume()`'s internal signature still takes `parsed` directly (it needs `parsed['raw_text']` for the raw-fallback prompt path, which isn't part of the `CandidateProfile` schema), and `generate_docx()` is unchanged. Extending `CandidateProfile` further should happen only where it removes a real duplication, not speculatively.

### Rule tables (`rules/`)

`CERT_REQUIREMENTS` and `SEA_SERVICE_MIN` (rank → required certs / minimum sea-service months) live in `rules/cert_rules.py`. `RANK_KEYWORDS` and `CERT_ALIASES` (fallback rank-detection keywords, certificate-name alias lists) live in `rules/aliases.py`. Plain Python dicts, not YAML/JSON — that migration is only worth it once these are actually being edited by non-engineers.

**Do not change point values or thresholds in `config.py`, `pipeline/scorer.py`, or `pipeline/maritime.py` without flagging it first** — they were tuned against the 269-resume validation set. If a rule table in `rules/` needs a value change, run `pipeline/batch_processor.py` against the validation set afterward and compare `data/outputs/batch_results.csv` before/after.

## Commands

```bash
# Run the ATS pipeline on a single resume (CLI)
venv/bin/python main.py <resume_file_path> [job_description]

# Example with a custom job description
venv/bin/python main.py data/test_resumes/ShreyaBansal_Resume_v2.pdf "Seeking Python developer with FastAPI experience"

# Batch-process the validation set (regression check after any rules/ or scoring change)
venv/bin/python pipeline/batch_processor.py

# Run the API locally
venv/bin/uvicorn api:app --reload --port 8000

# Run tests
venv/bin/python -m pytest tests/
```

## Scoring engine design

Two independent scorers run on every resume.

**ATS Score — /100** (`pipeline/scorer.py`)
- **Format (40 pts)**: single-column layout, no tables/images, standard fonts, contact parseability, no special characters, active voice, quantified achievements. `score_format()`.
- **Sections (20 pts)**: presence and quality of Summary, Experience, Education, Skills, Certifications. `score_sections()` checks **both** `parsed['sections']` and a raw-text heading scan — the raw-text path exists specifically so re-scoring a generated DOCX (Stage 7, no `parsed` dict available in the same shape) still works. Both paths are covered in `tests/test_scorer.py`.
- **Keywords (40 pts)**: blended score — 70% weight on raw JD-keyword coverage (stopword-filtered), 30% weight on TF-IDF cosine similarity. Pure cosine under-scores short JDs against long resumes; pure keyword-count over-rewards stuffing. `score_keywords()`.

**Maritime Score — /65 base + /20 bonus** (`pipeline/maritime.py`)
- **Certificate coverage (25 pts)**: rank-specific requirement table (`rules/cert_rules.py`) — e.g. Third Officer needs CoC + STCW + ENG1, Chief Officer additionally needs GMDSS. Alias matching (`rules/aliases.py`) handles real-world phrasing variance — INDOS/CDC numbers count as ENG1 proxies, since Indian seafarers' resumes rarely state "ENG1" explicitly but registration implies it.
- **Sea service (20 pts)**: `estimate_sea_service()` — extracted from the PDF's service-record table first (sign-on/sign-off date pairs, most reliable), falling back to regex MM/YYYY date-range parsing against `parsed['sections']['experience']` when there's no PDF to read (re-scoring a generated DOCX, or a DOCX was uploaded originally). Never silently zeroes out — see "Known-fixed bugs."
- **Vessel type match (10 pts)** and **rank detection (10 pts)**.
- **Bonus tier (0-20 pts)**: differentiates candidates who all hit the scoring ceiling — sea-service depth beyond minimum, vessel-type diversity, full cert coverage, senior-rank premium. Without this, a large share of candidates cluster at the same maximum score with no way to rank them.

An unrecognized rank (not a `KeyError`) falls back to `['STCW', 'ENG1']` required certs and a 6-month minimum via `.get(rank, default)` throughout — this is intentional graceful degradation, covered in `tests/test_maritime.py`.

## AI rewriting (`services/ai_client.py`)

Groq API, `qwen/qwen3.6-27b` (`config.GROQ_MODEL` — see "Known-fixed bugs" for why this is no longer Llama 3.3 70B). Three safety layers, none of which trust the LLM blindly:
1. **Raw-text fallback** triggers when parsed sections are too thin (common on heavily-garbled two-column resumes) — sends the LLM unprocessed text to reconstruct from instead.
2. **Post-generation validation** (`api.py::_validate_rewrite_output`) checks for placeholder text, empty sections, low skill counts, missing summary.
3. **Deterministic minimum-quality patch** (`enforce_minimum_quality`) pads skill lists and fixes certificate formatting without ever inventing facts.

**Retry/backoff**: `_call_groq_with_retry()` retries `APIConnectionError`, `APITimeoutError`, and `RateLimitError` with exponential backoff (1s/2s/4s, 3 retries) before giving up. Non-transient errors (auth, bad request) propagate immediately — retrying those would just waste time before failing the same way. The existing JSON-parse retry loop in `rewrite_resume()` is separate and unchanged. Retry count is threaded back via `result['_groq_retry_count']` and consumed by `api.py`'s metrics recording (popped off before the dict is used further).

**`MARITIME_JDS` is permanent, not a fallback.** Seafarers submit one resume to many manning agencies/vessels rather than tailoring per posting — there is no per-candidate job description to ever collect. `target_rank` (an optional `/ats/submit` form field) plus rank-detection is the complete feature; there is no "supply your own JD" path, and none should be added without an explicit product decision from MYC. Consequence worth remembering: the keyword-match sub-score always measures fit against a generic rank template, never a specific job posting — that's the intended ceiling of that metric, not a limitation to work around.

**`target_cruise_line` (optional `/ats/submit` form field) is a layer on top of `MARITIME_JDS`, not a replacement for it.** When MYC actually knows a specific cruise line a submission targets (real MYC use case: a recruiter tailoring one application), `rules/cruise_line_keywords.py`'s per-line keyword/cert/tone data (sourced from real MYC research into what each line's own ATS/recruiters emphasize) gets blended into the rewrite prompt alongside the rank-based template — never instead of it. `resolve_cruise_line()` normalizes free-text input and returns `''` for anything unrecognized, which the prompt-builder treats as "no cruise-line layer" rather than an error — an unrecognized/misspelled line name degrades gracefully to plain rank-based behavior. This is vocabulary/emphasis guidance for phrasing the candidate's own already-grounded experience, never a new source of facts — Rule 0 (grounding) and Rule 10 (never invent certs/experience) stay in force regardless of which line is targeted, and the cruise-line prompt block says so explicitly.

Note: `api.py`'s `/health/deep` endpoint does its own lightweight `from groq import Groq` connectivity ping, independent of `services/ai_client.py` — a minimal, isolated use for health-checking, not a second pipeline-logic import site.

## API reference (`api.py`)

All endpoints except `/` and `/health` require an `X-API-Key` header matching `MYC_API_KEY`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Service info, links to docs/health |
| POST | `/ats/submit` | Upload a PDF/DOCX resume (+ optional `job_description`, `target_rank`, `target_cruise_line`). Returns `job_id` immediately; processing runs in `BackgroundTasks`. Dedup by SHA-256 within 24h. |
| POST | `/ats/score` | Stages 1-4 only (extract/parse/ATS score/maritime score) — no AI rewrite, no Groq dependency, no `job_id`/polling. Synchronous, returns the full result in one response. Same file validation/rate-limit/auth posture as `/ats/submit`. Calls the exact same underlying functions `run_pipeline()` does for its "before" scores — see "Known-fixed bugs" for how that's actually verified, not just claimed. |
| GET | `/ats/status/{job_id}` | Poll every 2-3s: `queued`/`processing`/`done`/`failed`, `progress`, `current_stage`. |
| GET | `/ats/result/{job_id}` | Full before/after ATS + maritime scores and grades, issues, recommendations, missing certs, keyword data, download URL. |
| GET | `/ats/download/{job_id}` | Downloads the optimised DOCX. 410 if past the 48h TTL. |
| DELETE | `/ats/job/{job_id}` | Deletes the job record and output files — GDPR right-to-erasure. |
| POST | `/ats/cleanup` | Deletes all jobs past their TTL. Wire to cron (see `crontab_example.txt`). |
| GET | `/ats/admin/metrics` | Aggregate stats — see "Observability" below. |
| GET | `/health` | No auth. Disk space + job count, for load balancers. |
| GET | `/health/deep` | Auth required. Groq connectivity ping + DB + disk checks, for monitoring alerts. |

`/ats/result/{job_id}` returns `maritime_score_before`/`maritime_grade_before` and `maritime_score_after`/`maritime_grade_after` as distinct, independently-computed values (plus `maritime_score`/`maritime_grade` back-compat aliases pointing at the before-value) — see "Known-fixed bugs."

## Observability

Structured JSON logging throughout (`api.py`'s `PiiFilter` + `stage()` pattern) — strips email/phone/passport/CDC/INDOS patterns from every log line before it's written. Extended (not replaced) with:
- **Per-stage duration**: the `stage()` closure in `run_pipeline()` times each stage and logs+records it when the next stage starts (or the job finishes/fails).
- **Groq retry count per job** and a **running Groq failure count**, via an in-memory `_Metrics` counter (thread-safe).

`GET /ats/admin/metrics` returns: jobs processed/done/failed and avg ATS/maritime score deltas (from the `Job` table — durable, accurate across the whole process's history), plus Groq call/retry/failure counts and avg stage durations (from the in-memory counters).

**Caveat, stated in the response itself**: the in-memory figures reset on process restart and are **not** aggregated across `uvicorn --workers 2` — each worker process has its own counters. The Job-table figures (jobs processed, score deltas) are accurate process-wide since they hit the shared SQLite DB. If per-worker metrics fragmentation becomes a real problem, that's a trigger to revisit — not before.

## Environment variables

Copy `env_template.txt` to `.env` (never commit `.env`).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MYC_API_KEY` | Yes | — | Shared-secret auth for every `X-API-Key`-protected endpoint. Generate with `openssl rand -hex 32`. Service refuses all requests (503) if unset. |
| `GROQ_API_KEY` | Yes | — | Groq API key for the rewrite stage and `/health/deep`. **Rotate this** if it was ever present in an uploaded audit zip or committed by mistake — check `git log -p -- '*.env'` and any archived exports. |
| `MAX_FILE_MB` | No | `5` | Upload size limit. |
| `JOB_TTL_HOURS` | No | `48` | How long output files + job records are retained before `/ats/cleanup` deletes them. This is the PII retention boundary — do not extend without a data-retention reason, and don't add long-term storage of intermediate pipeline stages (extracted text, raw parses) elsewhere either; structured logging covers debugging needs. |
| `TMP_DIR` | No | `/tmp/ycm-ats` | Scratch space for uploaded files before/during processing. |
| `ALLOWED_ORIGINS` | No | `*` | CORS allow-list, comma-separated. |
| `MAX_CONCURRENT_JOBS` | No | `5` | Max resumes processing at once, service-wide — `submit()` returns 503 (not silent queueing) once `_try_acquire_job_slot()`'s atomic check against the real `jobs.db` (status IN `queued`/`processing`) is at capacity. Enforced correctly across `uvicorn --workers N` since the check reads the one shared database, not per-process memory — confirmed via a real `--workers 2` load test (see "Known-fixed bugs"). Distinct from the 10/minute per-IP rate limit on `/ats/submit` (via `slowapi`), which only throttles new submissions, not concurrent in-flight jobs — that limiter's state is still per-worker-process (real ceiling with 2 workers is ~2x the configured value), a deliberately accepted tradeoff given this service has one shared `MYC_API_KEY` across all of MYC's traffic. |

`config.py` also defines (not env vars — code constants, tuned against the validation set, don't change without flagging): `GROQ_MODEL` (`qwen/qwen3.6-27b` — `llama-3.3-70b-versatile` was decommissioned by Groq, see "Known-fixed bugs"), `FORMAT_WEIGHT`/`SECTION_WEIGHT`/`KEYWORD_WEIGHT` (40/20/40), and `SCORE_EXCELLENT`/`SCORE_GOOD`/`SCORE_PASS` (85/75/60).

## Deployment

`deploy.sh` (tested on Ubuntu 22.04, Python 3.11) does the following — run it on the target VPS:

1. Creates/activates `venv/`, installs `requirements.txt`.
2. Downloads the spaCy `en_core_web_lg` model if not already present (large — several hundred MB; this step is slow on first deploy).
3. Verifies `.env` exists and `MYC_API_KEY` has actually been changed from the placeholder — refuses to start otherwise.
4. Creates `data/outputs/` and the tmp upload dir.
5. Starts `uvicorn api:app` with 2 workers, bound to `0.0.0.0:8000`.

Manual steps `deploy.sh` does **not** cover:
- Putting a reverse proxy (nginx/Caddy) in front of port 8000 for TLS — this repo assumes ngrok or a VPS-level proxy handles that; it's not baked into `deploy.sh`.
- Installing the nightly cleanup cron job — see `crontab_example.txt`; add it with `crontab -e` on the deploy target, pointing `X-API-Key` at the real `MYC_API_KEY`.
- Restarting on boot / process supervision (systemd unit, pm2, supervisor) — not included; pick whichever your VPS provider's convention is, since none is currently checked in.
- Sharing the API key + endpoint URL with MYC's developer once deployed.

Re-running `deploy.sh` is safe (idempotent): it won't re-download the spaCy model if present, and won't overwrite an existing `.env`.

## Backup / restore — `data/jobs.db`

`data/jobs.db` is SQLite in WAL mode. The `Job` table holds no raw resume text/PII beyond what's listed in `env_template.txt`'s TTL note (name/rank/sea-months/cert-coverage metadata, not full parsed content) and expires via `/ats/cleanup`, so this is operational-continuity backup, not a PII archive.

**Backup** (safe to run while the API is live, WAL mode handles concurrent readers):
```bash
sqlite3 data/jobs.db ".backup data/jobs.db.bak"
```
Avoid a plain `cp` while the server is running — WAL mode means `jobs.db-wal`/`jobs.db-shm` may hold uncommitted state that a raw file copy won't capture consistently; `.backup` handles this correctly.

**Restore**:
```bash
# Stop the API first
cp data/jobs.db.bak data/jobs.db
rm -f data/jobs.db-wal data/jobs.db-shm   # stale WAL/SHM from the old process, if present
# Restart the API
```

**Output files** (`data/outputs/{job_id}/`) are not part of the DB backup — if you need to preserve in-flight download links across a restore, back up `data/outputs/` alongside `jobs.db` before the TTL sweep runs. Given the 48h TTL, this is rarely worth doing outside of active incident recovery.

## Troubleshooting

**Groq is down / timing out**
- `/health/deep` will report `"groq": "error: ..."` — check this first before assuming a job-level bug.
- Individual jobs retry transient failures automatically (1s/2s/4s backoff, 3 retries — see "AI rewriting" above). A sustained outage still surfaces as `rewritten['error'] == 'API_CALL_FAILED'` after retries exhaust; `run_pipeline()` falls back to preserving the original content rather than failing the whole job (`issues_fixed: ['AI rewrite unavailable — original content preserved']`), so candidates still get a DOCX, just without the AI rewrite.
- Check `GET /ats/admin/metrics` → `groq_failure_rate` to see whether this is one job or a pattern.

**Disk full**
- `/ats/submit` checks free disk space up-front (`MIN_DISK_FREE_MB = 200`) and returns `507 Insufficient disk space` rather than accepting an upload it can't process.
- `/health` and `/health/deep` report `disk_free_mb`/`disk_ok` — wire an alert to these.
- Most likely cause: `/ats/cleanup` isn't actually running on cron (check `crontab_example.txt` is installed) — expired job output isn't being swept. `data/outputs/` and `TMP_DIR` are the two places to check disk usage first.

**Malformed or scanned (image-only) resume upload**
- `pipeline/extractor.py::extract_pdf` returns `structural_issues: ['SCANNED_PDF_DETECTED_OCR_NEEDED']` when extracted text is under 100 characters — this isn't a crash, it's a low-confidence signal that scores very low on Format (-15 pts) and likely fails downstream sections/keyword scoring too, but the job still completes.
- A genuinely corrupt/unreadable file raises inside `extract_pdf`/`extract_docx` and is caught, returning `structural_issues: ['PDF_READ_ERROR: ...']` / `['DOCX_READ_ERROR: ...']` — again, not a job crash.
- `run_pipeline()` only hard-fails the job (`status: 'failed'`) if `extracted['raw_text']` is completely empty after extraction (`ValueError('Could not extract text...')`) — check `job.error_detail` via `/ats/status/{job_id}` for this case.
- OCR itself is out of scope — there's no OCR fallback pipeline; a scanned PDF is flagged, scored low, and left to the candidate to fix (re-upload a text-layer PDF).

**Job stuck in `processing`**
- Since `BackgroundTasks` runs in-process (no separate worker/queue — see "Architecture"), a job stuck in `processing` almost always means the worker process died mid-job (OOM, crash, manual restart) without reaching the `except`/`finally` block that would mark it `failed`.
- There is currently no automatic sweeper that requeues or times out stuck jobs (a known open item — whether a monitoring/requeue system exists elsewhere or needs building is still an unresolved product/eng decision, not something to build speculatively here). Until that's resolved: identify stuck jobs with `SELECT job_id, current_stage, created_at FROM jobs WHERE status='processing' AND created_at < datetime('now', '-1 hour')`, and either manually mark them `failed` via a one-off script or delete them via `DELETE /ats/job/{job_id}` so the candidate can resubmit.
- Check server logs around the job's `job_id` for the last stage it reached (`"stage":"..."` log lines) to diagnose which pipeline stage hung.

## Testing

`tests/test_scorer.py` and `tests/test_maritime.py` — pure-function coverage for the two highest-value, lowest-I/O files. Run with `venv/bin/python -m pytest tests/`. Not chasing full coverage; these two files were prioritized because they're pure functions with no I/O and carry the tuned scoring logic that must not silently regress.

After any change to `rules/`, `pipeline/scorer.py`, or `pipeline/maritime.py`: run the test suite, then run `pipeline/batch_processor.py` against the real validation set (`data/resume_file_list.txt`, gitignored — populated locally, not in this repo) and diff `data/outputs/batch_results.csv` against a pre-change baseline.

## Known-fixed bugs (worth knowing the history of)

- **Maritime re-score bug**: `maritime_score_after` used to be a copy-paste of `maritime_score_before` — Stage 7 never actually re-ran the maritime scorer on the rewritten output. Fixed by re-running `maritime_score()` on the regenerated DOCX and storing the result separately. This also surfaced a second bug: `estimate_sea_service()` only read sea months from a PDF path, so re-scoring a DOCX (no PDF) silently zeroed sea months out. Fixed with a text-based fallback (`extract_sea_months_from_text`) that parses the same MM/YYYY date-range pattern directly from `parsed['sections']['experience']`.
- **`stamper.py` removed permanently**: it applied an MYC watermark by converting the generated DOCX to PDF and overlaying an image — which violates the pipeline's own rule against graphics/images on the ATS-facing document (R7) and would degrade the score the pipeline just optimised. It was never called from `api.py` or `main.py` even before removal; this was dead code cleanup, not a behavior change.
- **`detect_rank()` misattributed a supervisor's/referee's rank to the candidate**: it scanned the whole resume body for any rank keyword with no concept of whose rank it was — a junior candidate's own bullet mentioning their senior officer ("Coordinated with the Chief Engineer to...", a References-section footer naming a referee's rank, possessive "Chief Engineer's guidance" phrasing) could outrank their correctly-stated own title. Confirmed on a live batch run against the full 269-resume set: **41 candidates (15%)** had an inflated rank, almost all Chief Officer/Chief Engineer when the real rank was junior. Fixed by checking the candidate's own header/title line first (the authoritative self-declaration), before ever falling back to a full-document scan; the fallback scan still strips known third-party-mention shapes as defense-in-depth. Also closed real rank-title gaps surfaced by the same audit: "Fourth/Third/Second Assistant Engineer" (US-convention titles) and "`<Rank>` Deck Officer" weren't recognized at all.
- **`data/jobs.db` schema silently drifted from the `Job` model**: `Base.metadata.create_all(engine)` only creates tables that don't exist yet — it never alters an existing one when a column is added to the model. The live file was missing `maritime_grade_before` entirely (a partial-migration artifact from the maritime re-score bug fix above), which would have raised `sqlite3.OperationalError` on every request touching the Job table — i.e. every endpoint. Caught by actually running `api.py` end-to-end for the first time (see the rate-limiting note below). Fixed the live file with an additive `ALTER TABLE`, and added `_ensure_schema_columns()` (runs at startup, reconciles the live table against the model via `PRAGMA table_info`, only ever adds columns, never drops/alters) so this class of drift self-heals instead of recurring silently.
- **Rate limiting (`slowapi`, 10/minute on `/ats/submit`) was never actually run**: the commit that added it only ran a `py_compile` syntax check, since that environment had neither `fastapi` nor `slowapi` installed. Installed and exercised for real via `TestClient` in `tests/test_api.py` — confirmed working (11th+ request in a minute correctly returns 429). Also surfaced the schema-drift bug above, since a real request against the real `data/jobs.db` was the first thing that ever actually happened. Separately, nothing bounded concurrent in-flight jobs (the rate limit only throttles new submissions) — added `MAX_CONCURRENT_JOBS` (see "Environment variables") as a lightweight in-memory guard, not a job queue.
- **`MAX_CONCURRENT_JOBS`'s original in-memory `_ConcurrencyGuard` was per-worker-process, silently doubling the real ceiling under `deploy.sh`'s `--workers 2`**: confirmed with a real load test — an actual `uvicorn --workers 2` process, `MAX_CONCURRENT_JOBS=2`, hit with a burst of concurrent `/ats/submit` requests — showed 4 jobs running concurrently, not 2. Fixed by replacing the in-memory counter with `_try_acquire_job_slot()`: an atomic `BEGIN IMMEDIATE` check-and-insert against the real `jobs.db` (counting rows with status `queued`/`processing`), so the limit is enforced by the one database every worker process already shares instead of by any single process's memory. Re-ran the same `--workers 2` load test after the fix: exactly 2 concurrent jobs, matching the configured limit. The 10/minute rate limiter has the same per-worker doubling and was deliberately left as-is — it's a much lower-stakes tradeoff for a service with one shared `MYC_API_KEY` across all of MYC's traffic, and `slowapi`/`limits` has no built-in SQLite storage backend to fix it the same way without meaningfully more custom code.
- **Extraction (Stage 1) had no wall-clock bound at all**: confirmed with a real ~800MB zlib decompression bomb (built from a <1MB PDF, comfortably under `MAX_FILE_MB`) that hung `extract_pdf()` for 90+ real seconds at ~100% CPU with zero sign of completing. Fixed with `_extract_with_timeout()`, which runs `extract_text()` in a disposable child process and forcibly terminates it past `EXTRACTION_TIMEOUT_SECONDS` (30s) — actually reclaiming the CPU/memory, not just giving up on waiting. Two subtler bugs surfaced while building this fix, both worth knowing about since they're easy to reintroduce: (1) forcing `multiprocessing`'s `'fork'` start method unconditionally reproduced a real hang on macOS, where forking a process after heavy C-extension/Objective-C-runtime imports (spaCy, numpy) is a known hazard — fixed by using the platform default instead (`'fork'` on Linux, the real deploy target, stays fast; `'spawn'` on macOS is slower but safe); (2) calling `proc.join(timeout)` *before* reading the result `multiprocessing.Queue` deadlocked on any real result over the OS pipe buffer (~64KB) — a completely benign, non-malicious 975-page PDF (~65KB of real extracted text) was being wrongly killed and reported as a timeout. Fixed by reading the queue *with* the timeout instead, which drains the pipe as the child writes to it. Side effect worth knowing: on macOS, every extraction now pays a real ~3-13s `spawn` re-import cost per call (Linux's `fork` doesn't have this cost) — `tests/test_api.py`'s rate-limiter test had to be changed to mock out `BackgroundTasks.add_task` after this made 12 real sequential `/ats/submit` calls span longer than slowapi's 60s rolling window, causing the limiter to (correctly, given the real timing) never fire.
- **`GROQ_MODEL` (`llama-3.3-70b-versatile`) was decommissioned by Groq** (the second such decommissioning — see this same variable's history) — confirmed via a real call (404 `model_not_found`) and a real `client.models.list()` showing it absent entirely. Every real candidate submission was silently falling back to "AI rewrite unavailable — original content preserved" until this was caught. Switched to `qwen/qwen3.6-27b` (see config.py's `GROQ_MODEL` comment for the full reasoning — the closest OpenAI-family replacement, `openai/gpt-oss-120b`, doesn't fit this account's real 8000 TPM on-demand-tier cap for this app's real prompt sizes; qwen's tokenizer is dramatically more efficient for this content and does). This is a genuinely different kind of model (a reasoning model whose `<think>` trace shares the same `.content` field unless `response_format={'type': 'json_object'}` is set, and whose JSON schema compliance is real but far less reliable call-to-call than the old model's), which surfaced a cluster of new, real, cross-candidate bugs found by direct manual review of real generated output (not the same 2-3 repeatedly-checked candidates — Gilfred/Ankur/Abdullah/Aashish Kumar all confirmed independently), all fixed together:
  - **Rank/title was never rendered under the candidate's name at all** — confirmed `content.get('rank')` was never referenced anywhere in `pipeline/generator.py`, on any candidate, regardless of model. Not a regression from the model switch; just newly surfaced by the same review pass. Fixed by adding the missing paragraph.
  - **Certifications/Languages sections had unrelated resume content bleeding in** ("Strengths", "Hobbies", "References" — including a referee's name and phone number ending up in a candidate's own Languages section) — root cause was in `pipeline/parser.py`, not the AI rewrite: `SECTION_HEADERS` had no entry for these common headings at all, so `split_sections()` silently appended their content to whatever real section preceded them, for every resume with these headings, regardless of model. Fixed by adding a `'other'` category so they get a heading of their own to land under instead.
  - **`education`/`documents`/`languages` frequently missing from qwen's own JSON output entirely** (not truncated — confirmed `finish_reason == 'stop'` on repeated real calls) — `education` wasn't previously defaultable (missing it was treated as a truncation signal, exhausting all 3 retries before falling back and losing the ENTIRE rewrite over one non-critical field). Added `enforce_education_completeness()`/`enforce_language_completeness()` (mirroring the existing `enforce_cert_completeness` restore-don't-invent pattern) and made `education` defaultable like `documents`/`languages` always were.
  - **Vessel names given in a separate field qwen invented (`vessels_served`/`vessels`, unstable even in name call-to-call) instead of inside the bullets**, where Rule 2 (VESSEL DETAILS MANDATORY) requires them — `generate_docx()` only ever reads `bullets`, so real vessel data was present in the raw response but never rendered. Fixed with a substring-matched (not exact-key) fold-in, given the field name isn't stable.
  - **A completeness-net-restored document (Passport/CDC/Visa) stayed in "certifications" instead of being reclassified into "documents"** — `_reclassify_document_like_certs` already existed and ran, but only once, *before* `enforce_cert_completeness` had a chance to append anything. Fixed by re-running it after appending.
  - **`enforce_minimum_quality`'s `has_real_exp` check crashed (`'str' object has no attribute 'get'`) on 3 of 18 candidates in a real randomized-sample batch run** when `experience_bullets` contained a plain string instead of a dict — unlike a similar check a few lines below it, this one had no `isinstance(e, dict)` guard. Fixed at both the immediate crash site and, as defense-in-depth, in `_normalize_rewrite_result` itself (converts a bare string entry into a minimal real dict rather than leaving it to crash whatever reads it next).
  - **The Groq-outage fallback (`api.py`, triggered whenever `rewrite_resume()` returns an error for ANY reason) hardcoded `'experience_bullets': []`** despite `issues_fixed` claiming "original content preserved" — confirmed on a real candidate whose rewrite failed on a real TPM rate-limit hit: a correctly-extracted 8-row gridded service-record table produced a completely empty Sea Service Record in the output. This was never a table-extraction bug. Fixed with `_build_fallback_experience_bullets()`, which reconstructs real entries from the raw extracted section text (one entry per pipe-delimited table row when the source has a real gridded table) instead of discarding it.
  - This account's Groq tier has a real, confirmed 8000 TPM cap that reasoning-model token overhead (reasoning tokens compete with the JSON output for the same budget) makes a genuine structural constraint, not something `max_tokens` tuning alone can fully solve — a real, busier resume's prompt tokens alone can already consume most or all of the budget. `_is_tpm_rate_limit_error()` now retries this specific case (confirmed via `isinstance()` that Groq returns it as a plain `APIStatusError`, not `RateLimitError`, so it previously bypassed retry entirely) — this and the fallback fix above are the actual safety net for a candidate whose resume is too large for this tier, not a `max_tokens` value.
- **Real MYC-supplied rank/certification research (not guessed) closed genuine gaps in `rules/aliases.py`/`rules/cert_rules.py`**, additive only — nothing that already had an established meaning was remapped, and a real full-269-candidate batch diff confirmed zero rank-detection changes and zero score decreases for any existing candidate. `RANK_KEYWORDS['chief officer']`/`['second engineer']` gained recognition for cruise-line title variants (`staff captain`, `1st engineer`/`senior 1st engineer`/`hotel engineer`/`first assistant engineer`) that map to the SAME organizational tier the existing cargo/tanker-oriented names already cover — confirmed via MYC's own rank documentation, including their explicit "4th Engineer (no 4th engineer in cruise ships)" note, which is what confirms the cruise ladder is the same length, just shifted by one name. A new `electrical cadet` rank tier was added (mirroring `deck cadet`/`engine cadet` exactly — STCW+ENG1, 0-month minimum) since there was previously no ETO-track cadet entry at all. `extract_certificates()`'s detection-only `extra_certs` list (see `pipeline/maritime.py`) gained SSO/BRM/BTM/ERM/DCE/RFPNW/RFPEW/Certificate of Proficiency — display/detection only, deliberately NOT added to `CERT_REQUIREMENTS`, so none of this can silently change a cert-coverage score. Deliberately NOT touched: remapping `2nd Engineer`/`3rd Engineer`/`1st Officer` strings (they already have an established meaning), and Safety Officer/Environment Officer (no clear existing-tier mapping — would mean inventing sea-service numbers without real basis) — both flagged, not silently guessed.
- **`POST /ats/score` added as a standalone, Groq-free endpoint** exposing Stages 1-4 only (extract/parse/ATS score/maritime score) — these stages are deterministic and were already proven stable across the full 269-resume batch independent of Stage 5-6's AI-rewrite instability (model decommissioning, TPM limits, fabrication, truncation — see the `qwen` entry above). Synchronous (no `BackgroundTasks`/`job_id`/polling, since there's no Groq call to wait on), same security posture as `/ats/submit` (API key auth, 10/minute rate limit, file validation), and confirmed via a real test to call the exact same `extract_text`/`parse_resume`/`score_resume`/`maritime_score` functions `run_pipeline()` calls for its "before" scores and return identical numbers for the same file — not just "structurally similar code", an actual behavioral equality check.
- **Cruise-line-specific keyword/cert tailoring** (`rules/cruise_line_keywords.py`, real data sourced from MYC cruise-line research) added as an **optional layer on top of** the existing rank-based `MARITIME_JDS` system, via an optional `target_cruise_line` form field on `/ats/submit` — parallel to how `target_rank` already works, and explicitly NOT a replacement for rank-based matching (still permanent by design, see "AI rewriting" above). `resolve_cruise_line()` normalizes free-text input and returns `''` for anything unrecognized rather than erroring. Confirmed via real tests: the default (no cruise line) prompt is byte-for-byte identical to the pre-feature prompt; a recognized line's keywords appear and a different line's don't leak in; an invalid/misspelled line degrades gracefully to the same byte-for-byte default prompt. The cruise-line prompt block explicitly restates Rule 0 (grounding) and Rule 10 (never invent) — this is vocabulary/emphasis guidance for the candidate's own real experience, never a new source of facts.

## Explicitly out of scope (revisit only on a real trigger, not preference)

- Celery/Redis job queue — until concurrent long-running jobs measurably queue/block on `BackgroundTasks`.
- PostgreSQL — until SQLite WAL's safe concurrency ceiling is actually hit.
- Multi-tenancy, billing, OAuth, SDKs — no measured need; one client, one developer.
- A second AI provider / generic AI-provider abstraction — until a real second use-case requires it.
- Per-candidate job description upload — `MARITIME_JDS` rank templates are permanent by design (see "AI rewriting" above), not a stopgap.
- OCR for scanned resumes — flagged and scored low, not auto-corrected.
- Hotel/Entertainment/Casino department scoring — real certification data for these exists in MYC's research (`Certifications Resume - Sheet1`), but every rule table (`CERT_REQUIREMENTS`, `SEA_SERVICE_MIN`, `RANK_KEYWORDS`) is built exclusively around Deck/Engine/ETO officer ranks, and MYC's actual placement scope for these departments hasn't been confirmed — trigger is an explicit MYC decision to place candidates into these departments through this system, not the data existing.
- A second, richer visual-template variant (vs. the current ATS-strict single-column output) — raised as a real open question from evidence that actual cruise-line application portals (JobTrain, BrassRing) are human-reviewed structured forms, not pure keyword-parsing bots, so a human-reviewer-optimized format might out-perform the ATS-strict one for those specific channels. Design decision pending explicit confirmation, not something to build speculatively — trigger is MYC confirming which channels are actually human-reviewed vs. bot-parsed and whether dual output is worth the added generation/testing surface.

## Roadmap

- Custom spaCy NER model fine-tuned on the growing resume corpus — replaces regex-based rank/cert/vessel extraction once the labeled dataset is large enough and the rules-based approach demonstrably underperforms it. `training/prelabel.py` is the current data-prep step toward this.
- Placement-outcome prediction once 500+ MYC placement records accumulate.
- PostgreSQL + Celery/Redis migration, if/when concurrent load exceeds SQLite WAL's safe ceiling.
- Human-in-the-loop review queue for resumes flagged by output validation before delivery to candidates.
