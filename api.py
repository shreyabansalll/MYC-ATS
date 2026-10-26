# api.py — Production-hardened FastAPI service for Marine Your Career ATS
# Embedded in MYC app. Candidates upload resume, get issues + optimised DOCX back.
#
# Security:   API key auth on all endpoints
# Validation: file size, type, content checks
# Privacy:    PII stripped from logs, temp files cleaned immediately
# Resilience: timeouts, disk checks, concurrent-safe job handling
# Logging:    structured JSON logs, no PII in output
#
# Run: uvicorn api:app --host 0.0.0.0 --port 8000 --workers 2

import os
import re
import ipaddress
import hmac
import uuid
import copy
import shutil
import hashlib
import logging
import json
import time
import sqlite3
import threading
import multiprocessing
import queue
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import (
    FastAPI, UploadFile, File, Form, Response,
    BackgroundTasks, HTTPException, Depends, Request
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, event
from sqlalchemy.orm import DeclarativeBase, Session
from starlette.datastructures import UploadFile as StarletteUploadFile
import sentry_sdk

from config import OUTPUT_DIR
from pipeline.extractor import extract_text, extract_photo
from pipeline.parser import parse_resume
from services.ai_client import rewrite_resume
from pipeline.generator import generate_docx
from models.candidate_profile import build_candidate_profile
from generic_ats.pdf_classic import generate_classic_pdf
from services.two_layer import (
    maritime_stub_for_profile,
    rewrite_quality_from_improvement,
    score_layers,
    score_layers_after_rewrite,
    should_retry_rewrite,
)

# ── Logging — structured JSON, NO PII ────────────────────────────────

_PII_PATTERNS = [
    re.compile(r'[\w.+-]{4,}@[\w-]+\.\w+'),          # email
    re.compile(r'[+]?\d[\d\s\-().]{8,}\d'),           # phone
    re.compile(r'\b[A-Z]\d{7,8}\b'),                   # passport
    re.compile(r'\b\d{2}[A-Z]{2}\d{5,7}\b'),          # CDC
    re.compile(r'\b\d{2}[A-Z]{2}\d{4}\b'),            # INDOS
]


def redact_pii(text: str) -> str:
    """
    Strips email/phone/passport/CDC/INDOS patterns. Shared by PiiFilter
    (log output) and anything persisted to the DB or returned via the API
    — error messages aren't exempt from the same privacy guarantee just
    because they're stored instead of logged.
    """
    for pat in _PII_PATTERNS:
        text = pat.sub('[REDACTED]', text)
    return text


class PiiFilter(logging.Filter):
    """Strips PII patterns from log messages — see redact_pii() above."""

    def filter(self, record):
        record.msg = redact_pii(str(record.getMessage()))
        record.args = ()
        return True


def _setup_logging():
    handler = logging.StreamHandler()
    handler.addFilter(PiiFilter())
    fmt = logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}'
    )
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Silence noisy libraries
    logging.getLogger('pdfminer').setLevel(logging.ERROR)
    logging.getLogger('pdfplumber').setLevel(logging.ERROR)

_setup_logging()
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────

API_KEY          = os.getenv('MYC_API_KEY', '')
ADMIN_API_KEY    = os.getenv('MYC_ADMIN_KEY', '')
DASHBOARD_ACCESS_KEY = os.getenv('DASHBOARD_ACCESS_KEY', '')
MAX_FILE_MB      = int(os.getenv('MAX_FILE_MB', '5'))
MAX_FILE_BYTES   = MAX_FILE_MB * 1024 * 1024
ALLOWED_EXTS     = {'pdf', 'docx'}
JOB_TTL_HOURS    = int(os.getenv('JOB_TTL_HOURS', '48'))   # auto-delete outputs after 48h
MIN_DISK_FREE_MB = 200                                        # refuse uploads if disk low

TMP_DIR = os.getenv('TMP_DIR', '/tmp/ycm-ats')
os.makedirs(TMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

if not API_KEY:
    log.warning('"MYC_API_KEY not set — all requests will be rejected"')

if not ADMIN_API_KEY:
    # Not a hard requirement — /ats/admin/metrics falls back to MYC_API_KEY
    # below so this doesn't lock out deployments that haven't set it up
    # yet, but that means the client-facing key (the one embedded in
    # MYC's app, more exposed than a pure backend secret) also grants
    # access to internal operational metrics (job counts, score deltas,
    # Claude failure rates) unless this is set.
    log.warning('"MYC_ADMIN_KEY not set — /ats/admin/metrics falls back to MYC_API_KEY, '
                'the same key client requests use. Set MYC_ADMIN_KEY to a distinct value."')

# ── Error tracking (Sentry) ──────────────────────────────────────────
# Optional — same "warn, don't crash at import" pattern as MYC_API_KEY/
# ANTHROPIC_API_KEY above. Without SENTRY_DSN set, the service runs exactly
# as before this was added (sentry_sdk.init() is simply never called,
# so every sentry_sdk.capture_exception() call below is a no-op).

SENTRY_DSN = os.getenv('SENTRY_DSN', '')


def _sentry_before_send(event, hint):
    """
    Same PII guarantee redact_pii()/PiiFilter already give logs and
    persisted error_detail — an exception's message or values can
    contain raw file-read errors, and those aren't exempt from the
    privacy guarantee just because they're headed to Sentry instead of
    the log stream or the DB.
    """
    if event.get('message'):
        event['message'] = redact_pii(event['message'])
    for exc in event.get('exception', {}).get('values', []):
        if exc.get('value'):
            exc['value'] = redact_pii(exc['value'])
    return event


if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        before_send=_sentry_before_send,
        send_default_pii=False,
        include_local_variables=False,
    )
    log.info('"sentry_enabled":"true"')
else:
    log.warning('"SENTRY_DSN not set — error tracking disabled, exceptions only go to logs"')

# ── Database ──────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'jobs.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={'check_same_thread': False},
)

# Enable WAL mode for concurrent read/write safety
@event.listens_for(engine, 'connect')
def _set_wal(dbapi_conn, _):
    dbapi_conn.execute('PRAGMA journal_mode=WAL')
    dbapi_conn.execute('PRAGMA foreign_keys=ON')


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = 'jobs'

    job_id                = Column(String(36), primary_key=True)
    status                = Column(String(20), default='queued')   # queued|processing|done|failed
    progress              = Column(Integer, default=0)
    current_stage         = Column(String(100), default='Queued')
    error_detail          = Column(Text, nullable=True)

    # Scores
    ats_score_before      = Column(Integer, nullable=True)
    ats_grade_before      = Column(String(20), nullable=True)
    ats_score_after       = Column(Integer, nullable=True)
    ats_grade_after       = Column(String(20), nullable=True)
    maritime_score_before = Column(Integer, nullable=True)
    maritime_grade_before = Column(String(20), nullable=True)
    maritime_score_after  = Column(Integer, nullable=True)
    maritime_grade_after  = Column(String(20), nullable=True)
    improvement           = Column(Integer, nullable=True)

    rewrite_quality       = Column(String(20), nullable=True)

    # Output paths — relative, not absolute (portable)
    output_docx           = Column(String(500), nullable=True)
    output_pdf            = Column(String(500), nullable=True)

    # JSON fields
    issues_json           = Column(Text, nullable=True)
    recommendations_json  = Column(Text, nullable=True)
    missing_certs_json    = Column(Text, nullable=True)
    issues_fixed_json     = Column(Text, nullable=True)
    keyword_data_json     = Column(Text, nullable=True)

    # Metadata — NO PII stored
    rank_detected         = Column(String(50), nullable=True)
    sea_months            = Column(Integer, nullable=True)
    cert_coverage         = Column(String(10), nullable=True)
    file_hash             = Column(String(64), nullable=True)   # SHA256 of input, for dedup

    created_at            = Column(DateTime, default=datetime.utcnow)
    completed_at          = Column(DateTime, nullable=True)
    expires_at            = Column(DateTime, nullable=True)      # when to delete output files


Base.metadata.create_all(engine)


def _ensure_schema_columns():
    """
    Base.metadata.create_all() only creates tables that don't exist yet
    -- it never alters an EXISTING table when the Job model gains a new
    column. Real bug confirmed here, caught only by actually running a
    real request against the real data/jobs.db for the first time (the
    prior rate-limiting work that touched this file was syntax-checked
    only, never executed, since this environment didn't have slowapi/
    fastapi installed until now): the live file was missing
    'maritime_grade_before' entirely -- a partial-migration artifact
    from when maritime scoring was split into separate before/after
    columns (see CLAUDE.md's "maritime re-score bug" history) -- so
    EVERY query touching the Job table (i.e. every endpoint) would have
    raised sqlite3.OperationalError: no such column against the real
    production database. There's no migration framework in this
    project (Alembic would be disproportionate complexity for a single-
    VPS/single-client deployment), so this is the lightweight
    equivalent: reconcile the live table's actual columns (via PRAGMA
    table_info) against what the ORM model expects, and ADD any that
    are missing. Purely additive -- never drops or alters an existing
    column -- so it can't lose data, only prevent this exact class of
    silent drift from recurring the next time a column is added to Job
    without a matching manual ALTER on the live file.
    """
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql('PRAGMA table_info(jobs)').fetchall()}
        model_columns = {col.name: col for col in Job.__table__.columns}
        missing = set(model_columns) - existing
        for name in missing:
            col = model_columns[name]
            col_type = col.type.compile(engine.dialect)
            conn.exec_driver_sql(f'ALTER TABLE jobs ADD COLUMN {name} {col_type}')
            log.warning(f'"schema_drift_repaired":"{name}","column_type":"{col_type}"')
        if missing:
            conn.commit()


_ensure_schema_columns()


def get_db() -> Session:
    return Session(engine)


def update_job(db: Session, job_id: str, **kwargs):
    job = db.get(Job, job_id)
    if job:
        for k, v in kwargs.items():
            setattr(job, k, v)
        db.commit()

# ── In-memory observability counters ────────────────────────────────────
# Score deltas and job counts live in the Job table already — these cover
# what doesn't: per-stage durations and Claude retry/failure counts. Reset on
# process restart, and NOT shared across `--workers N` (each worker is a
# separate process) — acceptable for a single-client, single-VPS deployment;
# revisit only if that stops being true (see architecture doc trigger table).

class _Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.claude_calls         = 0
        self.claude_retries_total = 0
        self.claude_failures      = 0
        self.stage_durations      = defaultdict(list)   # stage label -> [seconds, ...]

    def record_stage(self, label: str, seconds: float):
        with self._lock:
            self.stage_durations[label].append(seconds)

    def record_llm_attempt(self, retries: int, failed: bool):
        with self._lock:
            self.claude_calls += 1
            self.claude_retries_total += retries
            if failed:
                self.claude_failures += 1


_metrics = _Metrics()

# ── Auth ──────────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)


async def require_api_key(key: Optional[str] = Depends(_api_key_header)):
    if not API_KEY:
        raise HTTPException(503, 'Service not configured — MYC_API_KEY missing')
    if not key or key != API_KEY:
        log.warning('"Rejected request with invalid API key"')
        raise HTTPException(403, 'Invalid or missing API key')
    return key


async def require_admin_key(key: Optional[str] = Depends(_api_key_header)):
    """
    Separate from require_api_key so the client-facing key (embedded in
    MYC's app) doesn't also grant access to internal operational metrics.
    Falls back to MYC_API_KEY if MYC_ADMIN_KEY isn't set — see the
    startup warning above — so this degrades to the old (shared-key)
    behavior rather than locking out a deployment that hasn't set the
    new variable, rather than a hard failure.
    """
    expected = ADMIN_API_KEY or API_KEY
    if not expected:
        raise HTTPException(503, 'Service not configured — MYC_API_KEY missing')
    if not key or key != expected:
        log.warning('"Rejected admin request with invalid API key"')
        raise HTTPException(403, 'Invalid or missing API key')
    return key

# ── App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title='Marine Your Career — ATS API',
    description='Resume scoring and optimisation for seafarers. Embedded in MYC app.',
    version='2.0.0',
    docs_url='/docs',          # disable in prod by setting to None
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv('ALLOWED_ORIGINS', '*').split(','),
    allow_methods=['GET', 'POST'],
    allow_headers=['X-API-Key', 'Content-Type'],
)

# ── Rate limiting ─────────────────────────────────────────────────────
# Per-IP, in-memory (default slowapi backend) — resets on process
# restart and isn't shared across `--workers N`, same acceptable
# single-VPS tradeoff as the in-memory _Metrics counters above. 10/minute
# is a starting value for a resume-upload endpoint (uploads take real
# processing time; this guards against abuse, not normal single-user
# traffic) — not tuned against real usage, adjust once real traffic
# patterns are known.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Concurrency guard ────────────────────────────────────────────────
# The rate limiter above only throttles new SUBMISSIONS per IP per
# minute — it does nothing to bound how many already-accepted jobs can
# be actively PROCESSING at once. A burst near the rate-limit ceiling,
# or several different IPs submitting close together (each individually
# well under 10/minute), could still queue an unbounded number of CPU/
# memory-heavy jobs (spaCy NLP, PDF parsing, a live Claude HTTP call) onto
# BackgroundTasks' shared thread pool at the same time — real risk of
# resource exhaustion on a single small VPS, which is the literal shape
# of "don't want the system to crash if multiple people upload
# together." In-memory, per-worker-process — same acceptable single-VPS
# tradeoff as the rate limiter and _Metrics counters above (not shared
# across `--workers N`, so the real ceiling with 2 workers is roughly
# 2x this value). Deliberately a simple counter+lock, not Celery/Redis
# — CLAUDE.md's explicit position is that a real job queue is only
# warranted once concurrent jobs measurably queue/block on
# BackgroundTasks; this is the proportionate guard rail for that
# trigger, not a queue itself — jobs over the cap are rejected
# immediately with a clear 503 rather than silently piling up.
MAX_CONCURRENT_JOBS = int(os.getenv('MAX_CONCURRENT_JOBS', '5'))


def _try_acquire_job_slot(job_id: str, file_hash: str) -> bool:
    """
    Atomically checks whether fewer than MAX_CONCURRENT_JOBS rows are
    currently 'queued'/'processing' and, if so, inserts the new Job row
    in the *same* transaction — so the check and the insert can never
    race against another process doing the same thing.

    Replaces a prior in-memory, per-process threading.Lock-guarded
    counter. Real multi-worker load test (`uvicorn --workers 2`,
    MAX_CONCURRENT_JOBS=2) confirmed that counter was per-worker-process:
    4 jobs ran concurrently, not 2 — deploy.sh's default --workers 2
    silently doubled the real resource-exhaustion ceiling the guard
    exists to enforce (unlike the 10/min rate limiter's equivalent
    doubling, which is low-stakes for a single-shared-API-key client and
    already documented as an accepted tradeoff, this one directly
    undermines its own stated purpose — protecting one small VPS's
    CPU/memory from a burst of concurrent spaCy/Claude-heavy jobs). Backing
    the check with jobs.db instead of process memory makes the limit a
    property of the one database every worker process already shares.

    Uses the stdlib sqlite3 module directly rather than the SQLAlchemy
    engine/Session used elsewhere in this file, specifically to issue a
    real `BEGIN IMMEDIATE` — this takes SQLite's single writer lock for
    the whole check-then-insert, so two processes attempting this at the
    same instant are serialized rather than both reading a stale count
    and both inserting. There is deliberately no separate "release" step
    anywhere else in this file: once run_pipeline moves a row to
    status='done'/'failed' via the ordinary update_job() path, it drops
    out of the queued/processing count on its own — the row's status
    *is* the slot, so there's no separate counter that a missed release
    on some exit path could leak.

    A real sqlite3.OperationalError (lock-wait timeout under extreme
    concurrent load) is treated the same as "no slot available" rather
    than propagating — the caller's 503 ("at capacity, try again") is an
    honest response either way, and this guarantees the DB being briefly
    busy is never surfaced to the candidate as a raw 500.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.isolation_level = None  # autocommit off -- we drive BEGIN/COMMIT/ROLLBACK ourselves
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'processing')"
        ).fetchone()[0]
        if count >= MAX_CONCURRENT_JOBS:
            conn.execute('ROLLBACK')
            return False
        conn.execute(
            'INSERT INTO jobs (job_id, status, progress, current_stage, file_hash, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (job_id, 'queued', 0, 'Queued', file_hash, datetime.utcnow().isoformat(sep=' ')),
        )
        conn.execute('COMMIT')
        return True
    except sqlite3.OperationalError as e:
        log.warning(f'"concurrency_slot_check_db_busy":"{str(e)}"')
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        return False
    finally:
        conn.close()

# ── Validators ────────────────────────────────────────────────────────

def _check_disk_space():
    # Uploads land in TMP_DIR first, then outputs go to OUTPUT_DIR — check
    # both, since they can be different filesystems (e.g. TMP_DIR on tmpfs).
    for directory in (TMP_DIR, OUTPUT_DIR):
        stat = shutil.disk_usage(directory)
        free_mb = stat.free // (1024 * 1024)
        if free_mb < MIN_DISK_FREE_MB:
            raise HTTPException(507, f'Insufficient disk space ({free_mb}MB free on {directory})')


def _validate_file(contents: bytes, filename: str) -> str:
    """Validates file size, extension, and magic bytes. Returns extension."""
    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(413, f'File exceeds {MAX_FILE_MB}MB limit')

    ext = (filename or '').rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(415, f'Only PDF and DOCX accepted, got .{ext}')

    # Magic byte check — PDF starts with %PDF, DOCX is a ZIP (PK)
    if ext == 'pdf' and not contents.startswith(b'%PDF'):
        raise HTTPException(415, 'File does not appear to be a valid PDF')
    if ext == 'docx' and not contents[:2] == b'PK':
        raise HTTPException(415, 'File does not appear to be a valid DOCX')

    return ext


def _file_hash(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _check_duplicate(db: Session, file_hash: str) -> Optional[str]:
    """Returns existing job_id if same file processed recently (24h)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    existing = (
        db.query(Job)
        .filter(
            Job.file_hash == file_hash,
            Job.status == 'done',
            Job.created_at >= cutoff,
        )
        .first()
    )
    return existing.job_id if existing else None

# ── Output validation ─────────────────────────────────────────────────

def _build_fallback_experience_bullets(parsed: dict, rank: str) -> list:
    """
    Real bug fixed: the LLM-outage fallback below used to hardcode
    'experience_bullets': [] unconditionally -- despite issues_fixed
    claiming "original content preserved" -- silently producing a
    completely EMPTY Sea Service Record / Work Experience section
    whenever the AI rewrite fails for ANY reason (LLM outage, a real
    account-level rate limit, auth failure), not just a total,
    obvious outage. Confirmed on a real candidate (Ashish Kumar) whose
    rewrite failed on a real, live 413 TPM rate-limit error: the
    generated resume had zero experience rows despite a real, correctly
    extracted 8-row gridded service-record table sitting the whole time
    in parsed['sections']['experience'] -- this was never a table-
    extraction bug, it was this fallback discarding data that had
    already been extracted successfully.

    Builds real entries from the raw extracted experience/service-record
    text rather than inventing structure (there's no LLM available here
    to safely restructure it). Pipe-delimited rows -- the gridded-table
    rendering shape from extractor.py's _render_table_as_text ("Company
    Name | Vessel Name | Type of vessel | GRT | Rank | Sign On | Sign
    Off | Number of days sailed") -- become one real entry per row, using
    the actual extracted company/vessel/dates. Anything else (a resume
    without a gridded service-record table, so no pipe-delimited rows at
    all) becomes a single entry with the raw section text as its
    bullets, so at minimum nothing is silently reduced to zero rows.
    """
    experience_text = parsed.get('sections', {}).get('experience', '') if isinstance(parsed, dict) else ''
    if not experience_text or not experience_text.strip():
        return []

    role_title = rank.title() if rank else 'Seafarer'
    lines = [l.strip() for l in experience_text.split('\n') if l.strip()]

    entries = []
    for line in lines:
        if line.count('|') < 5:
            continue
        parts = [p.strip() for p in line.split('|')]
        if parts[0].lower() in ('company name', 'company'):
            continue  # header row
        company, vessel = parts[0], parts[1]
        sign_on  = parts[5] if len(parts) > 5 else ''
        sign_off = parts[6] if len(parts) > 6 else ''
        entries.append({
            'role':    role_title,
            'company': company,
            'dates':   f'{sign_on} - {sign_off}'.strip(' -'),
            'bullets': [f'Served aboard {vessel}.'] if vessel else [],
        })
    if entries:
        return entries

    return [{
        'role':    role_title,
        'company': '',
        'dates':   '',
        'bullets': lines[:5],
    }]


def _validate_rewrite_output(result: dict, source_raw: str) -> list[str]:
    """
    Checks generated content for quality issues.
    Returns list of warning strings. Empty = clean output.
    """
    warnings_out = []

    # Check for placeholder text
    placeholders = ['Actual Issuing Authority', 'Issuing Authority', '| Year', 'See service record']
    for cert in result.get('certifications', []):
        if any(p in cert for p in placeholders):
            warnings_out.append(f'Placeholder text in certifications: {cert[:60]}')

    # Check experience not empty
    if not result.get('experience_bullets'):
        warnings_out.append('Experience section empty in generated output')

    # Check skills minimum
    if len(result.get('skills', [])) < 10:
        warnings_out.append(f'Low skill count in output: {len(result.get("skills", []))}')

    # Check summary exists
    if not result.get('summary', '').strip():
        warnings_out.append('Summary missing in generated output')

    return warnings_out

# ── Extraction timeout guard ─────────────────────────────────────────
# Real gap confirmed by a deliberate test: an 800MB zlib decompression
# bomb (a <1MB PDF, comfortably under MAX_FILE_MB) hung extract_pdf()
# for 90+ real seconds with zero sign of completing, and kept consuming
# ~100% CPU indefinitely with nothing to stop it. Stage 1 previously had
# no wall-clock bound at all.
#
# A signal.alarm()-based timeout doesn't work here: run_pipeline executes
# on BackgroundTasks' worker thread, not the main thread, and Python's
# signal handling only works on the main thread of the main interpreter
# -- calling signal.alarm() from here would raise ValueError. A
# thread-based timeout (ThreadPoolExecutor.result(timeout=...)) doesn't
# genuinely fix it either: it only stops the CALLER from waiting, it
# can't forcibly kill a still-running thread, so the runaway CPU/memory
# consumption would continue regardless of whether the caller gave up
# waiting on it. Only a separate OS process can actually be killed and
# have its resources reclaimed -- hence multiprocessing here, not
# threading.

EXTRACTION_TIMEOUT_SECONDS = 30


def _extract_worker(result_queue: multiprocessing.Queue, file_path: str, ext: str):
    """Runs in a disposable child process -- see _extract_with_timeout()."""
    try:
        result_queue.put(('ok', extract_text(file_path, ext)))
    except Exception as e:
        result_queue.put(('error', str(e)))


def _extract_with_timeout(file_path: str, ext: str) -> dict:
    """
    Runs extract_text() in a fresh child process and forcibly kills it if
    it's still running past EXTRACTION_TIMEOUT_SECONDS -- actually
    reclaiming whatever memory/CPU a pathological input (decompression
    bomb, deeply malformed/nested object structure) is consuming, not
    just giving up on waiting for it.

    Reads the result queue WITH the timeout (queue.get(timeout=...)),
    rather than proc.join(timeout) followed by a separate queue.get().
    That ordering looks equivalent but isn't: multiprocessing.Queue is
    pipe-backed with a limited OS buffer (~64KB), and a child that has
    put() a larger payload blocks writing the remainder until something
    reads it. join()-before-get() never reads it, so a sufficiently
    large (but completely successful, non-pathological) extraction
    result deadlocks the child against the parent's own wait -- this was
    confirmed for real: a 975-page benign PDF (~65KB of extracted text,
    just over the pipe buffer) was being wrongly killed and reported as
    EXTRACTION_TIMEOUT by an earlier version of this function that
    joined first, even though extract_pdf() itself completes that exact
    file in ~1.3s standalone. Reading the queue first drains the pipe as
    the child writes to it, so this only ever times out for a genuinely
    stuck extraction, never a large-but-finished one.

    Also deliberately uses multiprocessing's PLATFORM DEFAULT start
    method (no explicit 'fork' override) -- forcing 'fork' unconditionally
    reproduced a second, separate real hazard: forking this process
    (after api.py's full import chain -- spaCy, numpy, etc. -- has
    already loaded) can deadlock on macOS, a known fork-safety hazard
    that's exactly why Python changed macOS's default start method from
    'fork' to 'spawn' in 3.8. Linux's default remains 'fork', which is
    the fast, safe path in production per deploy.sh's Ubuntu target --
    this just doesn't override whichever default is actually safe for
    the platform it's running on.
    """
    ctx   = multiprocessing.get_context()
    result_queue = ctx.Queue()
    proc  = ctx.Process(target=_extract_worker, args=(result_queue, file_path, ext))
    proc.start()

    try:
        status, payload = result_queue.get(timeout=EXTRACTION_TIMEOUT_SECONDS)
    except queue.Empty:
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join()
        return {
            'raw_text':          '',
            'structural_issues': [f'EXTRACTION_TIMEOUT: exceeded {EXTRACTION_TIMEOUT_SECONDS}s '
                                   '-- file may be a decompression bomb or a pathologically malformed document'],
            'is_ocr':            False,
        }

    # The child should exit almost immediately after the put() above --
    # bounded join as a final cleanup, not a second timeout window.
    proc.join(5)
    if proc.is_alive():
        proc.terminate()
        proc.join()

    if status == 'ok':
        return payload
    if status == 'error':
        return {
            'raw_text':          '',
            'structural_issues': [f'EXTRACTION_ERROR: {payload}'],
            'is_ocr':            False,
        }

    # Unreachable in practice (status is always 'ok' or 'error' from
    # _extract_worker), kept only so a future change to that function
    # can't silently fall through here without a signal.
    return {
        'raw_text':          '',
        'structural_issues': [f'EXTRACTION_UNKNOWN_STATUS: {status!r}'],
        'is_ocr':            False,
    }

def _rewrite_or_fallback(
    parsed, all_issues, tailored_jd, rank, candidate_profile,
    target_cruise_line, overlay, strict=False,
):
    rewritten = rewrite_resume(
        parsed=parsed,
        issues=all_issues,
        job_description=tailored_jd,
        rank=rank,
        license_rank=candidate_profile.license_rank,
        sea_months=candidate_profile.sea_months,
        target_cruise_line=target_cruise_line if overlay else '',
        strict=strict,
        generic_mode=not overlay,
    )
    retries = rewritten.pop('_llm_retry_count', 0) if isinstance(rewritten, dict) else 0
    failed = isinstance(rewritten, dict) and 'error' in rewritten
    if failed:
        log.warning(f'"rewrite_error":"{rewritten.get("error")}"')
        rewritten = {
            'summary': parsed.get('sections', {}).get('summary', ''),
            'experience_bullets': _build_fallback_experience_bullets(parsed, rank),
            'skills': parsed.get('skills', []),
            'certifications': [],
            'education': [],
            'documents': {},
            'languages': [],
            'technical_expertise': [],
            'issues_fixed': [AI_REWRITE_FALLBACK_ISSUE],
            'rank': rank,
        }
    return rewritten, failed, retries


AI_REWRITE_FALLBACK_ISSUE = 'AI rewrite unavailable — original content preserved'


def _inject_identity(rewritten, candidate_profile, rank, maritime_before):
    rewritten['name'] = candidate_profile.name
    rewritten['contact'] = candidate_profile.contact.model_dump()
    rewritten['rank'] = rank
    rewritten['sea_months'] = (maritime_before or {}).get('sea_months')
    rewritten['vessel_types'] = (maritime_before or {}).get('vessel_types', [])


def _write_ats_outputs(rewritten, job_id, out_dir):
    generate_docx(copy.deepcopy(rewritten), job_id=job_id)
    default_path = os.path.join(OUTPUT_DIR, f'{job_id}_ats_resume.docx')
    docx_path = os.path.join(out_dir, f'{job_id}_ats.docx')
    if os.path.exists(default_path):
        shutil.move(default_path, docx_path)
    pdf_path = os.path.join(out_dir, f'{job_id}_ats.pdf')
    generate_classic_pdf(rewritten, pdf_path)
    return docx_path, pdf_path


# ── Background pipeline ───────────────────────────────────────────────

def run_pipeline(
    job_id: str,
    file_path: str,
    ext: str,
    job_description: str,
    target_rank: str,
    target_cruise_line: str = '',
    career_track: str = '',
):
    """
    Full pipeline. Runs in BackgroundTasks thread.
    Updates job record at each stage. Cleans up temp files on exit.
    """
    import warnings
    warnings.filterwarnings('ignore', message='.*FontBBox.*')

    db = get_db()

    _stage_state = {'label': None, 'started': None}

    def _finish_open_stage():
        if _stage_state['label'] is not None:
            elapsed = time.monotonic() - _stage_state['started']
            _metrics.record_stage(_stage_state['label'], elapsed)
            log.info(
                f'"job_id":"{job_id}","stage_complete":"{_stage_state["label"]}",'
                f'"duration_s":{elapsed:.2f}'
            )
            _stage_state['label'] = None

    def stage(label: str, pct: int):
        _finish_open_stage()
        _stage_state['label']   = label
        _stage_state['started'] = time.monotonic()
        update_job(db, job_id, current_stage=label, progress=pct, status='processing')
        log.info(f'"job_id":"{job_id}","stage":"{label}","pct":{pct}')

    try:
        # ── 1. Extract ────────────────────────────────────────────────
        # Runs in a subprocess with a hard wall-clock timeout -- see
        # _extract_with_timeout()'s docstring for why (a real
        # decompression bomb confirmed to hang this stage indefinitely
        # with nothing previously bounding it).
        stage('Extracting text', 10)
        extracted = _extract_with_timeout(file_path, ext)

        if not extracted.get('raw_text', '').strip():
            # Real gap fixed, confirmed against real password-protected/
            # corrupt/zero-byte test files: extract_pdf() already
            # distinguishes these (PASSWORD_PROTECTED_PDF, PDF_READ_ERROR
            # with a real message, SCANNED_PDF_DETECTED_OCR_NEEDED) via
            # structural_issues, but this generic message discarded all of
            # that detail -- every failure shape reached the candidate as
            # the exact same unhelpful "file may be image-based or empty",
            # even a password-protected upload, which needs completely
            # different, actionable advice (remove the password) than a
            # genuinely corrupt file (re-export and re-upload) or an
            # actually-empty one.
            reason = '; '.join(extracted.get('structural_issues', [])) or 'unknown reason'
            raise ValueError(f'Could not extract text ({reason})')

        # ── 2. Parse ──────────────────────────────────────────────────
        stage('Parsing resume', 20)
        parsed = parse_resume(extracted['raw_text'])
        parsed['raw_text'] = extracted['raw_text']   # needed for raw fallback in rewriter

        # ── 3. Score (before) — Layer 1 always; Layer 2 if overlay on ─
        stage('Scoring original resume', 35)
        layers_before = score_layers(
            extracted, parsed,
            job_description=job_description,
            career_track=career_track,
            target_rank=target_rank,
            target_cruise_line=target_cruise_line,
            pdf_path=file_path,
            ext=ext,
        )
        ats_before = layers_before['generic']
        maritime_before = layers_before['maritime']
        overlay = layers_before['overlay']
        tailored_jd = layers_before['job_description']

        candidate_profile = build_candidate_profile(
            parsed,
            maritime_before if maritime_before is not None else maritime_stub_for_profile(parsed),
        )

        # ── 4. AI Rewrite (Claude) ────────────────────────────────────
        stage('Rewriting for ATS optimisation', 50)
        rank = target_rank or candidate_profile.rank_detected
        maritime_issues = maritime_before['issues'] if maritime_before else []
        all_issues = [redact_pii(i) for i in ats_before['issues'] + maritime_issues]

        rewritten, rewrite_failed, llm_retries = _rewrite_or_fallback(
            parsed, all_issues, tailored_jd, rank, candidate_profile,
            target_cruise_line, overlay, strict=False,
        )
        _metrics.record_llm_attempt(retries=llm_retries, failed=rewrite_failed)
        log.info(
            f'"job_id":"{job_id}","llm_retry_count":{llm_retries},'
            f'"llm_failed":{str(rewrite_failed).lower()},"overlay":{str(overlay).lower()}'
        )

        quality_warnings = _validate_rewrite_output(rewritten, extracted['raw_text'])
        if quality_warnings:
            log.warning(f'"job_id":"{job_id}","quality_warnings":{json.dumps(quality_warnings)}')

        _inject_identity(rewritten, candidate_profile, rank, maritime_before)

        # ── 5. Generate DOCX + classic single-column PDF ──────────────
        # /ats/download/{job_id} still serves the photo-free DOCX (existing
        # contract). PDF is additive. Photo DOCX stays in the job folder
        # without a public route (same product decision as before).
        stage('Generating ATS-clean DOCX', 65)
        out_dir = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(out_dir, exist_ok=True)
        docx_path, pdf_path = _write_ats_outputs(rewritten, job_id, out_dir)

        photo_bytes = extract_photo(file_path) if ext == 'pdf' else None
        if photo_bytes:
            generate_docx(
                copy.deepcopy(rewritten), job_id=job_id,
                include_photo=True, photo_bytes=photo_bytes,
            )
            photo_default_path = os.path.join(OUTPUT_DIR, f'{job_id}_ats_resume_photo.docx')
            if os.path.exists(photo_default_path):
                shutil.move(photo_default_path, os.path.join(out_dir, f'{job_id}_ats_photo.docx'))

        # ── 6. Re-score; retry once if generic ATS gained < 15 pts ────
        stage('Verifying improvement', 80)
        extracted2 = extract_text(docx_path, 'docx')
        parsed2 = parse_resume(extracted2['raw_text'])
        layers_after = score_layers_after_rewrite(
            rewritten, extracted2, parsed2,
            job_description=tailored_jd,
            career_track=career_track,
            target_rank=rank,
            target_cruise_line=target_cruise_line,
            overlay=overlay,
        )
        ats_after = layers_after['generic']
        maritime_after = layers_after['maritime']
        improvement = ats_after['total_score'] - ats_before['total_score']

        if should_retry_rewrite(improvement, rewrite_failed):
            stage('Retrying rewrite with stricter prompt', 72)
            retry_rewritten, retry_failed, retry_retries = _rewrite_or_fallback(
                parsed, all_issues, tailored_jd, rank, candidate_profile,
                target_cruise_line, overlay, strict=True,
            )
            _metrics.record_llm_attempt(retries=retry_retries, failed=retry_failed)
            if not retry_failed:
                _inject_identity(retry_rewritten, candidate_profile, rank, maritime_before)
                retry_docx, retry_pdf = _write_ats_outputs(retry_rewritten, job_id, out_dir)
                extracted_r = extract_text(retry_docx, 'docx')
                parsed_r = parse_resume(extracted_r['raw_text'])
                layers_retry = score_layers_after_rewrite(
                    retry_rewritten, extracted_r, parsed_r,
                    job_description=tailored_jd,
                    career_track=career_track,
                    target_rank=rank,
                    overlay=overlay,
                )
                retry_improvement = (
                    layers_retry['generic']['total_score'] - ats_before['total_score']
                )
                if layers_retry['generic']['total_score'] > ats_after['total_score']:
                    rewritten, docx_path, pdf_path = retry_rewritten, retry_docx, retry_pdf
                    ats_after, maritime_after = layers_retry['generic'], layers_retry['maritime']
                    improvement = retry_improvement
                    extracted2, parsed2 = extracted_r, parsed_r

        quality = rewrite_quality_from_improvement(improvement)
        log.info(
            f'"job_id":"{job_id}",'
            f'"score_before":{ats_before["total_score"]},'
            f'"score_after":{ats_after["total_score"]},'
            f'"improvement":{improvement},'
            f'"rewrite_quality":"{quality}"'
        )

        # ── 7. Save ───────────────────────────────────────────────────
        stage('Saving results', 95)
        maritime_score_before = maritime_before['maritime_score'] if maritime_before else None
        maritime_grade_before = maritime_before['maritime_grade'] if maritime_before else None
        maritime_score_after = maritime_after['maritime_score'] if maritime_after else None
        maritime_grade_after = maritime_after['maritime_grade'] if maritime_after else None
        update_job(
            db, job_id,
            status='done',
            progress=100,
            current_stage='Complete',
            completed_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=JOB_TTL_HOURS),

            ats_score_before=ats_before['total_score'],
            ats_grade_before=ats_before['grade'],
            ats_score_after=ats_after['total_score'],
            ats_grade_after=ats_after['grade'],
            maritime_score_before=maritime_score_before,
            maritime_grade_before=maritime_grade_before,
            maritime_score_after=maritime_score_after,
            maritime_grade_after=maritime_grade_after,
            improvement=improvement,
            rewrite_quality=quality,

            output_docx=docx_path,
            output_pdf=pdf_path,

            rank_detected=rank,
            sea_months=(maritime_before or {}).get('sea_months'),
            cert_coverage=(maritime_before or {}).get('cert_coverage'),

            issues_json=json.dumps(all_issues),
            recommendations_json=json.dumps((maritime_before or {}).get('recommendations', [])),
            missing_certs_json=json.dumps((maritime_before or {}).get('missing_certs', [])),
            issues_fixed_json=json.dumps(rewritten.get('issues_fixed', [])),
            keyword_data_json=json.dumps(ats_before.get('keyword_data', {})),
        )
        _finish_open_stage()

    except Exception as e:
        _finish_open_stage()
        log.error(f'"job_id":"{job_id}","error":"{str(e)}"')
        sentry_sdk.capture_exception(e)
        try:
            update_job(
                db, job_id,
                status='failed',
                progress=0,
                current_stage='Failed',
                error_detail=redact_pii(str(e)),   # traceback NOT stored — may contain PII
            )
        except Exception as db_err:
            # If even marking the job failed doesn't succeed (e.g. a locked
            # SQLite write under WAL contention), don't let that mask the
            # original error or leave the job silently stuck in "processing"
            # with no trace in the logs of why.
            log.error(f'"job_id":"{job_id}","error":"failed to record failure: {str(db_err)}"')
            sentry_sdk.capture_exception(db_err)
    finally:
        # Always clean up temp upload file
        try:
            os.remove(file_path)
        except OSError:
            pass
        db.close()
        # No explicit concurrency-slot release here — see
        # _try_acquire_job_slot()'s docstring. The slot is just this
        # row's status: once the update_job() calls above move it to
        # 'done' or 'failed', it stops counting on its own. (If a worker
        # process dies mid-job before either update_job() call above can
        # run at all, the row is left 'processing' and keeps counting
        # against capacity even across a restart — this is the exact
        # same "job stuck in processing" scenario CLAUDE.md's
        # Troubleshooting section already documents a manual remediation
        # for, not a new failure mode this change introduces.)

# ── Endpoints ─────────────────────────────────────────────────────────

def _dashboard_client_is_local(request: Request) -> bool:
    client_host = request.client.host if request.client else ''
    requested_host = request.url.hostname or ''
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    try:
        requested_host_is_local = ipaddress.ip_address(requested_host).is_loopback
    except ValueError:
        requested_host_is_local = requested_host.lower() == 'localhost'
    return is_loopback and requested_host_is_local


def _require_dashboard_origin(request: Request) -> None:
    origin = request.headers.get('origin')
    if origin:
        parsed_origin = urlsplit(origin)
        if (
            parsed_origin.scheme != request.url.scheme
            or parsed_origin.netloc.lower() != request.headers.get('host', '').lower()
        ):
            raise HTTPException(403, 'Cross-origin dashboard requests are not allowed')
    elif request.method == 'POST':
        raise HTTPException(403, 'Dashboard requests must originate from this page')


def _dashboard_session_token() -> str:
    return hmac.new(
        DASHBOARD_ACCESS_KEY.encode('utf-8'),
        b'marine-ats-dashboard-session-v1',
        hashlib.sha256,
    ).hexdigest()


def _require_dashboard_access(request: Request, *, allow_login: bool = False) -> None:
    is_local = _dashboard_client_is_local(request)
    _require_dashboard_origin(request)
    if is_local and not DASHBOARD_ACCESS_KEY:
        return
    if not DASHBOARD_ACCESS_KEY:
        raise HTTPException(
            403,
            'Remote dashboard access is disabled; configure DASHBOARD_ACCESS_KEY to enable team testing',
        )
    if allow_login:
        return

    expected_cookie = _dashboard_session_token()
    supplied_cookie = request.cookies.get('marine_dashboard_session', '')
    if not hmac.compare_digest(supplied_cookie, expected_cookie):
        raise HTTPException(401, 'Dashboard session is missing or expired')


@app.get('/dashboard', include_in_schema=False)
def dashboard(request: Request):
    """Serve the internal API testing dashboard from this API origin."""
    _require_dashboard_access(request, allow_login=True)
    return FileResponse(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'),
        media_type='text/html',
    )


@app.get('/dashboard/session', include_in_schema=False)
def dashboard_session(request: Request):
    _require_dashboard_access(request, allow_login=True)
    authenticated = bool(
        DASHBOARD_ACCESS_KEY
        and request.cookies.get('marine_dashboard_session')
        and hmac.compare_digest(
            request.cookies.get('marine_dashboard_session', ''),
            _dashboard_session_token(),
        )
    )
    local_trusted = _dashboard_client_is_local(request) and not DASHBOARD_ACCESS_KEY
    return {
        'required': bool(DASHBOARD_ACCESS_KEY),
        'authenticated': authenticated or local_trusted,
    }


@app.post('/dashboard/session', include_in_schema=False)
async def create_dashboard_session(request: Request):
    _require_dashboard_access(request, allow_login=True)
    if not DASHBOARD_ACCESS_KEY:
        raise HTTPException(404, 'Dashboard access key is not configured')
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid dashboard login request')
    access_key = payload.get('access_key') if isinstance(payload, dict) else None
    if not isinstance(access_key, str) or not hmac.compare_digest(
        access_key,
        DASHBOARD_ACCESS_KEY,
    ):
        raise HTTPException(403, 'Invalid dashboard access key')

    response = JSONResponse({'authenticated': True})
    response.set_cookie(
        'marine_dashboard_session',
        _dashboard_session_token(),
        max_age=8 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='strict',
        path='/',
    )
    return response


@app.api_route(
    '/dashboard-api/{api_path:path}',
    methods=['GET', 'POST'],
    include_in_schema=False,
)
async def dashboard_api_proxy(api_path: str, request: Request):
    """Forward authenticated dashboard requests using the server-side API key."""
    _require_dashboard_access(request)
    if not API_KEY:
        raise HTTPException(503, 'Service not configured — MYC_API_KEY missing')

    safe_path = '/' + api_path
    is_score_or_submit = safe_path in {'/ats/score', '/ats/submit'}
    is_job_read = re.fullmatch(r'/ats/(status|result|download)/[0-9a-fA-F-]{36}', safe_path)
    if not (
        (request.method == 'POST' and is_score_or_submit)
        or (request.method == 'GET' and is_job_read)
    ):
        raise HTTPException(404, 'Dashboard proxy route not found')

    multipart_chunks: list[bytes] = []
    request_headers = {'X-API-Key': API_KEY}
    if request.method == 'POST':
        async with request.form() as form:
            upload = form.get('file')
            if not isinstance(upload, StarletteUploadFile):
                raise HTTPException(422, 'A resume file is required')
            if upload.size is not None and upload.size > MAX_FILE_BYTES:
                raise HTTPException(413, f'File exceeds the {MAX_FILE_MB}MB limit')
            allowed_fields = {
                'job_description', 'target_rank', 'target_cruise_line', 'career_track',
            }
            data = [
                (key, value)
                for key, value in form.multi_items()
                if key in allowed_fields and isinstance(value, str)
            ]
            file_content = await upload.read()
            filename = (upload.filename or 'resume').replace('\\', '_').replace('"', '_')
            filename = filename.replace('\r', '_').replace('\n', '_')
            file_type = (
                'application/pdf' if filename.lower().endswith('.pdf')
                else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                if filename.lower().endswith('.docx')
                else 'application/octet-stream'
            )
            boundary = f'marine-ats-{uuid.uuid4().hex}'
            multipart_chunks = [
                (
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                ).encode('utf-8')
                + value.encode('utf-8')
                + b'\r\n'
                for key, value in data
            ]
            multipart_chunks.extend([
                (
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                    f'Content-Type: {file_type}\r\n\r\n'
                ).encode('utf-8'),
                file_content,
                b'\r\n',
                f'--{boundary}--\r\n'.encode('ascii'),
            ])
            request_headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        if 'format' in request.query_params:
            raise HTTPException(400, 'Unexpected query parameter')
    elif safe_path.startswith('/ats/download/'):
        invalid_params = set(request.query_params) - {'format'}
        if invalid_params:
            raise HTTPException(400, 'Unsupported download query parameter')

    transport = httpx.ASGITransport(app=app)

    async def upload_stream():
        for chunk in multipart_chunks or []:
            yield chunk

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url='http://marine-ats.internal',
        ) as client:
            upstream = await client.request(
                request.method,
                safe_path,
                params=request.query_params if request.method == 'GET' else None,
                content=upload_stream() if request.method == 'POST' else None,
                headers=request_headers,
            )
    except httpx.HTTPError as exc:
        log.error(f'"dashboard_proxy_error":"{redact_pii(str(exc))}"')
        raise HTTPException(502, 'Could not reach the ATS API handler')

    headers = {}
    if 'content-disposition' in upstream.headers:
        headers['Content-Disposition'] = upstream.headers['content-disposition']
    media_type = upstream.headers.get('content-type')
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=media_type,
        headers=headers,
    )


@app.get('/', summary='API root — welcome and documentation')
def root(request: Request):
    """Welcome endpoint. Links to API documentation and health check."""
    base = str(request.base_url).rstrip('/')
    return {
        'service': 'Marine Your Career - ATS API',
        'version': '2.0.0',
        'description': 'Resume scoring and optimisation for seafarers.',
        'documentation': f'{base}/docs',
        'dashboard': f'{base}/dashboard',
        'health_check': f'{base}/health',
        'endpoints': {
            'submit_resume': 'POST /ats/submit',
            'score_resume': 'POST /ats/score',
            'check_status': 'GET /ats/status/{job_id}',
            'get_results': 'GET /ats/result/{job_id}',
            'download_resume': 'GET /ats/download/{job_id}',
            'delete_job': 'DELETE /ats/job/{job_id}',
            'cleanup_expired': 'POST /ats/cleanup',
            'health': 'GET /health',
            'deep_health': 'GET /health/deep',
        },
        'auth': 'All endpoints require X-API-Key header',
    }


@app.post(
    '/ats/submit',
    summary='Upload resume and start ATS optimisation',
    dependencies=[Depends(require_api_key)],
)
@limiter.limit('10/minute')
async def submit(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description='PDF or DOCX resume'),
    job_description: str = Form(default='', description='Target job description (optional but improves keyword scoring)'),
    target_rank: str = Form(default='', description='Override rank detection (optional)'),
    target_cruise_line: str = Form(
        default='',
        description=(
            'Optional. When the submission targets a specific cruise line (e.g. "Disney", '
            '"Royal Caribbean", "Carnival"), layers that line\'s hiring keywords/cert emphasis '
            'into the AI rewrite on top of the existing rank-based template -- never a '
            'replacement for it. Unrecognized/misspelled names fall back gracefully to plain '
            'rank-based behavior, not an error. See rules/cruise_line_keywords.py for the full '
            'list of recognized lines.'
        ),
    ),
    career_track: str = Form(
        default='',
        description=(
            'Optional. If absent and a maritime rank is detected, Layer 2 (maritime) still '
            'runs so existing MYC uploads keep maritime scores. Pass "generic" to force '
            'Layer 1 only. Pass a rank/track (e.g. chief_officer, eto) to run both layers '
            'and tailor against that maritime JD template.'
        ),
    ),
):
    """
    Accepts PDF or DOCX resume. Returns job_id immediately.
    Processing runs in background — poll /ats/status/{job_id}.

    Returns existing result if same file submitted within 24 hours (dedup by SHA256).
    """
    # Disk space check
    _check_disk_space()

    # Read and validate file
    contents = await file.read()
    ext      = _validate_file(contents, file.filename or '')

    # Dedup check
    fhash = _file_hash(contents)
    db    = get_db()
    existing_id = _check_duplicate(db, fhash)
    db.close()
    if existing_id:
        log.info(f'"dedup_hit":"true","existing_job":"{existing_id}"')
        return {
            'job_id':   existing_id,
            'status':   'done',
            'cached':   True,
            'message':  'Same file processed recently — returning existing result',
        }

    # Concurrency check — a dedup hit above never reaches here, since
    # returning a cached result doesn't consume a processing slot at
    # all. _try_acquire_job_slot() atomically checks capacity AND
    # creates the 'queued' Job row in one DB transaction (see its
    # docstring) — there is no separate release step; the row's own
    # status is the slot, freed automatically once run_pipeline moves it
    # to 'done'/'failed'.
    job_id = str(uuid.uuid4())
    if not _try_acquire_job_slot(job_id, fhash):
        log.warning(f'"concurrency_limit_reached":"{MAX_CONCURRENT_JOBS}"')
        raise HTTPException(
            503,
            f'Server is at capacity processing other resumes (max {MAX_CONCURRENT_JOBS} '
            'concurrent) — please try again in a moment.',
        )

    tmp_path = os.path.join(TMP_DIR, f'{job_id}.{ext}')
    try:
        with open(tmp_path, 'wb') as f:
            f.write(contents)
    except Exception as e:
        log.error(f'"job_id":"{job_id}","error":"submit failed: {redact_pii(str(e))}"')
        sentry_sdk.capture_exception(e)
        # The job row already exists (the slot above IS the row) --
        # mark it failed rather than leaving it stuck 'queued' forever
        # with nothing ever going on to process it.
        db2 = get_db()
        try:
            update_job(db2, job_id, status='failed', current_stage='Failed',
                       error_detail='Could not save upload')
        finally:
            db2.close()
        raise HTTPException(500, 'Could not save upload — please try again')

    log.info(f'"job_id":"{job_id}","ext":"{ext}","size_kb":{len(contents)//1024}')

    background_tasks.add_task(
        run_pipeline, job_id, tmp_path, ext, job_description, target_rank,
        target_cruise_line, career_track,
    )

    return {'job_id': job_id, 'status': 'queued'}


@app.post(
    '/ats/score',
    summary='Score a resume — generic ATS always; maritime overlay when career_track/rank applies. No AI rewrite.',
    dependencies=[Depends(require_api_key)],
)
@limiter.limit('10/minute')
async def score(
    request: Request,
    file: UploadFile = File(..., description='PDF or DOCX resume'),
    job_description: str = Form(default='', description='Target job description (optional but improves keyword scoring)'),
    target_rank: str = Form(default='', description='Override rank detection (optional)'),
    target_cruise_line: str = Form(default='', description='Optional cruise line for overlay activation'),
    career_track: str = Form(default='', description='Optional. generic = Layer 1 only; maritime rank/track = both layers'),
):
    """
    Runs Stages 1-4 only (extract -> parse -> ATS score -> maritime
    score) -- the same underlying functions run_pipeline() calls for its
    "before" scores, just invoked directly and returned synchronously.
    No BackgroundTasks/job_id/polling: there's no LLM call to wait on,
    so this responds in the same request, typically in a couple of
    seconds. Does not persist a Job row or generate a DOCX -- this is a
    read-only scoring operation, not a submission.

    Same security posture as /ats/submit: API key auth, the same
    10/minute rate limit, the same file validation (extension/magic-
    byte/size via _validate_file), and the same PII redaction on
    anything returned.
    """
    _check_disk_space()

    contents = await file.read()
    ext      = _validate_file(contents, file.filename or '')

    tmp_id   = str(uuid.uuid4())
    tmp_path = os.path.join(TMP_DIR, f'{tmp_id}.{ext}')

    try:
        with open(tmp_path, 'wb') as f:
            f.write(contents)

        # Same _extract_with_timeout() subprocess-isolated, wall-clock-
        # bounded extraction /ats/submit uses -- a scoring-only endpoint
        # is exactly as exposed to a decompression-bomb-style upload as
        # the full pipeline is, so it needs exactly the same guard.
        extracted = _extract_with_timeout(tmp_path, ext)
        if not extracted.get('raw_text', '').strip():
            reason = '; '.join(extracted.get('structural_issues', [])) or 'unknown reason'
            raise HTTPException(422, f'Could not extract text ({reason})')

        parsed = parse_resume(extracted['raw_text'])
        parsed['raw_text'] = extracted['raw_text']

        layers = score_layers(
            extracted, parsed,
            job_description=job_description,
            career_track=career_track,
            target_rank=target_rank,
            target_cruise_line=target_cruise_line,
            pdf_path=tmp_path,
            ext=ext,
        )
        ats_result = layers['generic']
        maritime_result = layers['maritime']
        candidate_profile = build_candidate_profile(
            parsed,
            maritime_result if maritime_result is not None else maritime_stub_for_profile(parsed),
        )

        maritime_issues = maritime_result['issues'] if maritime_result else []
        issues = [redact_pii(i) for i in ats_result['issues'] + maritime_issues]

        log.info(
            f'"endpoint":"/ats/score","ats_score":{ats_result["total_score"]},'
            f'"maritime_score":{(maritime_result or {}).get("maritime_score")},'
            f'"overlay":{str(layers["overlay"]).lower()}'
        )

        return {
            'name':            candidate_profile.name,
            'ats_score':       ats_result['total_score'],
            'ats_grade':       ats_result['grade'],
            'maritime_score':  (maritime_result or {}).get('maritime_score'),
            'maritime_grade':  (maritime_result or {}).get('maritime_grade'),
            'overlay_active':  layers['overlay'],
            'rank_detected':   candidate_profile.rank_detected,
            'sea_months':      candidate_profile.sea_months,
            'cert_coverage':   (maritime_result or {}).get('cert_coverage'),
            'vessel_types':    (maritime_result or {}).get('vessel_types', []),
            'issues':          issues,
            'recommendations': (maritime_result or {}).get('recommendations', []),
            'missing_certs':   (maritime_result or {}).get('missing_certs', []),
            'keyword_data':    ats_result.get('keyword_data', {}),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f'"endpoint":"/ats/score","error":"{redact_pii(str(e))}"')
        sentry_sdk.capture_exception(e)
        raise HTTPException(500, 'Could not score resume — please try again')
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@app.get(
    '/ats/status/{job_id}',
    summary='Check processing progress',
    dependencies=[Depends(require_api_key)],
)
def status(job_id: str):
    """
    Poll this every 2-3 seconds from the app.
    Returns status: queued | processing | done | failed
    """
    db  = get_db()
    job = db.get(Job, job_id)
    db.close()

    if not job:
        raise HTTPException(404, 'Job not found')

    resp = {
        'job_id':        job.job_id,
        'status':        job.status,
        'progress':      job.progress,
        'current_stage': job.current_stage,
    }
    if job.status == 'failed':
        resp['error'] = job.error_detail
    return resp


@app.get(
    '/ats/result/{job_id}',
    summary='Get scores, issues and download URL',
    dependencies=[Depends(require_api_key)],
)
def result(job_id: str):
    """
    Full before/after scores, issues fixed, missing certs, keyword data.
    Only available once status == done.
    """
    db  = get_db()
    job = db.get(Job, job_id)
    db.close()

    if not job:
        raise HTTPException(404, 'Job not found')
    if job.status in ('queued', 'processing'):
        return JSONResponse(
            status_code=202,
            content={'detail': 'Still processing', 'progress': job.progress, 'stage': job.current_stage},
        )
    if job.status == 'failed':
        raise HTTPException(500, job.error_detail or 'Processing failed')

    def _load(field):
        try:
            return json.loads(field) if field else []
        except Exception:
            return []

    issues_fixed = _load(job.issues_fixed_json)
    ai_rewrite = AI_REWRITE_FALLBACK_ISSUE not in issues_fixed
    return {
        'job_id':               job.job_id,
        'ai_rewrite':           ai_rewrite,
        'note': (
            None if ai_rewrite else
            'Anthropic rewrite was unavailable (missing/invalid API key or provider error); '
            'original resume content was preserved.'
        ),

        # Scores
        'score_before':         job.ats_score_before,
        'grade_before':         job.ats_grade_before,
        'score_after':          job.ats_score_after,
        'grade_after':          job.ats_grade_after,
        'improvement':          job.improvement,
        'rewrite_quality':      job.rewrite_quality,
        'maritime_score_before': job.maritime_score_before,
        'maritime_grade_before': job.maritime_grade_before,
        'maritime_score_after':  job.maritime_score_after,
        'maritime_grade_after':  job.maritime_grade_after,
        # Back-compat aliases — historically pointed at the pre-rewrite value
        'maritime_score':       job.maritime_score_before,
        'maritime_grade':       job.maritime_grade_before,

        # Maritime details
        'rank_detected':        job.rank_detected,
        'sea_months':           job.sea_months,
        'cert_coverage':        job.cert_coverage,

        # Issues
        'issues':               _load(job.issues_json),
        'recommendations':      _load(job.recommendations_json),
        'missing_certs':        _load(job.missing_certs_json),
        'issues_fixed':         issues_fixed,
        'keyword_data':         _load(job.keyword_data_json),

        # Download — DOCX remains the existing contract; PDF is additive
        'download_url':         f'/ats/download/{job_id}',
        'download_url_pdf':     f'/ats/download/{job_id}?format=pdf',
        'completed_at':         job.completed_at.isoformat() if job.completed_at else None,
        'expires_at':           job.expires_at.isoformat()   if job.expires_at   else None,
    }


@app.get(
    '/ats/download/{job_id}',
    summary='Download the optimised DOCX resume',
    dependencies=[Depends(require_api_key)],
)
def download(job_id: str, format: str = 'docx'):
    """Returns the ATS-optimised DOCX by default. Pass format=pdf for the classic single-column PDF."""
    db  = get_db()
    job = db.get(Job, job_id)
    db.close()

    if not job:
        raise HTTPException(404, 'Job not found')
    if job.status != 'done':
        raise HTTPException(400, f'Job not complete — status: {job.status}')

    if job.expires_at and datetime.utcnow() > job.expires_at:
        raise HTTPException(410, 'Result has expired — please resubmit')

    want_pdf = (format or 'docx').lower() == 'pdf'
    if want_pdf:
        path = job.output_pdf
        if not path or not os.path.exists(path):
            raise HTTPException(410, 'PDF output expired or not found')
        return FileResponse(
            path=path,
            media_type='application/pdf',
            filename=f'MYC_ATS_Resume_{job_id[:8]}.pdf',
            headers={'Content-Disposition': f'attachment; filename="MYC_ATS_Resume_{job_id[:8]}.pdf"'},
        )

    if not job.output_docx or not os.path.exists(job.output_docx):
        raise HTTPException(410, 'Output file expired or not found')

    return FileResponse(
        path=job.output_docx,
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        filename=f'MYC_ATS_Resume_{job_id[:8]}.docx',
        headers={'Content-Disposition': f'attachment; filename="MYC_ATS_Resume_{job_id[:8]}.docx"'},
    )


@app.delete(
    '/ats/job/{job_id}',
    summary='Delete job and associated files (GDPR right to erasure)',
    dependencies=[Depends(require_api_key)],
)
def delete_job(job_id: str):
    """
    Permanently deletes job record and output files.
    Use this for GDPR right-to-erasure requests.
    """
    db  = get_db()
    job = db.get(Job, job_id)

    if not job:
        db.close()
        raise HTTPException(404, 'Job not found')

    # Delete output file
    if job.output_docx:
        out_dir = os.path.dirname(job.output_docx)
        try:
            shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass

    db.delete(job)
    db.commit()
    db.close()

    log.info(f'"job_deleted":"{job_id}"')
    return {'deleted': True, 'job_id': job_id}


@app.post(
    '/ats/cleanup',
    summary='Delete expired jobs and output files (run nightly)',
    dependencies=[Depends(require_api_key)],
)
def cleanup():
    """
    Deletes all jobs past their TTL. Call this from a cron job nightly.
    Frees disk space and ensures PII is not retained beyond 48 hours.
    """
    db      = get_db()
    now     = datetime.utcnow()
    expired = db.query(Job).filter(Job.expires_at < now).all()
    deleted = 0

    for job in expired:
        if job.output_docx:
            out_dir = os.path.dirname(job.output_docx)
            shutil.rmtree(out_dir, ignore_errors=True)
        db.delete(job)
        deleted += 1

    db.commit()
    db.close()

    log.info(f'"cleanup_deleted":{deleted}')
    return {'deleted_jobs': deleted, 'timestamp': now.isoformat()}


@app.get(
    '/ats/admin/metrics',
    summary='Aggregate pipeline metrics — jobs, score deltas, Claude health, stage durations',
    dependencies=[Depends(require_admin_key)],
)
def admin_metrics():
    """
    Jobs-processed and score-delta stats come from the Job table (durable).
    Claude retry/failure counts and stage durations come from in-memory
    counters (reset on restart, per-worker-process — see note in response).
    """
    db = get_db()
    try:
        total_jobs  = db.query(Job).count()
        done_jobs   = db.query(Job).filter(Job.status == 'done').all()
        failed_jobs = db.query(Job).filter(Job.status == 'failed').count()
    finally:
        db.close()

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    ats_deltas = [
        j.ats_score_after - j.ats_score_before
        for j in done_jobs
        if j.ats_score_after is not None and j.ats_score_before is not None
    ]
    maritime_deltas = [
        j.maritime_score_after - j.maritime_score_before
        for j in done_jobs
        if j.maritime_score_after is not None and j.maritime_score_before is not None
    ]

    claude_calls = _metrics.claude_calls
    claude_failure_rate = (
        round(_metrics.claude_failures / claude_calls, 4) if claude_calls else None
    )
    avg_stage_durations = {
        label: round(sum(durations) / len(durations), 2)
        for label, durations in _metrics.stage_durations.items()
    }

    return {
        'jobs_processed':           total_jobs,
        'jobs_done':                len(done_jobs),
        'jobs_failed':              failed_jobs,
        'avg_ats_score_delta':      _avg(ats_deltas),
        'avg_maritime_score_delta': _avg(maritime_deltas),
        'claude_calls':             claude_calls,
        'claude_retries_total':     _metrics.claude_retries_total,
        'claude_failures_total':    _metrics.claude_failures,
        'claude_failure_rate':      claude_failure_rate,
        'avg_stage_durations_seconds': avg_stage_durations,
        'note': (
            'claude_* and stage-duration figures are in-memory counters for '
            'this worker process only — they reset on restart and are not '
            'aggregated across `--workers N`. jobs_*/score-delta figures '
            'come from the Job table and are accurate process-wide.'
        ),
    }


@app.get('/health', summary='Health check')
def health():
    """Returns service health. No auth required — used by load balancers."""
    disk   = shutil.disk_usage(OUTPUT_DIR)
    free_mb = disk.free // (1024 * 1024)

    db = get_db()
    try:
        job_count = db.query(Job).count()
    except Exception:
        job_count = -1
    finally:
        db.close()

    return {
        'status':       'ok',
        'service':      'Marine Your Career ATS API',
        'version':      '2.0.0',
        'disk_free_mb': free_mb,
        'disk_ok':      free_mb >= MIN_DISK_FREE_MB,
        'total_jobs':   job_count,
        'timestamp':    datetime.utcnow().isoformat(),
    }


@app.get('/health/deep', summary='Deep health check', dependencies=[Depends(require_api_key)])
def health_deep():
    """Checks Claude connectivity and disk. Use for monitoring alerts."""
    checks = {}

    try:
        import anthropic
        import httpx
        from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
        ping = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            http_client=httpx.Client(trust_env=False, timeout=5.0),
        )
        ping.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1,
            messages=[{'role': 'user', 'content': 'ping'}],
        )
        checks['claude'] = 'ok'
    except Exception as e:
        checks['claude'] = f'error: {str(e)[:50]}'

    # DB
    try:
        db = get_db()
        db.query(Job).count()
        db.close()
        checks['db'] = 'ok'
    except Exception as e:
        checks['db'] = f'error: {str(e)[:50]}'

    # Disk
    disk = shutil.disk_usage(OUTPUT_DIR)
    free_mb = disk.free // (1024 * 1024)
    checks['disk_free_mb'] = free_mb
    checks['disk'] = 'ok' if free_mb >= MIN_DISK_FREE_MB else 'low'

    all_ok = all(v == 'ok' or isinstance(v, int) for v in checks.values())
    return {
        'status':   'ok' if all_ok else 'degraded',
        'checks':   checks,
        'timestamp': datetime.utcnow().isoformat(),
    }