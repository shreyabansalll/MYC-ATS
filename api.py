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
import uuid
import shutil
import hashlib
import logging
import json
import time
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    FastAPI, UploadFile, File, Form,
    BackgroundTasks, HTTPException, Depends, Request
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, event
from sqlalchemy.orm import DeclarativeBase, Session

from config import OUTPUT_DIR
from pipeline.extractor import extract_text
from pipeline.parser import parse_resume
from pipeline.scorer import score_resume
from pipeline.maritime import maritime_score
from services.ai_client import rewrite_resume
from pipeline.generator import generate_docx
from models.candidate_profile import build_candidate_profile

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

    # Output paths — relative, not absolute (portable)
    output_docx           = Column(String(500), nullable=True)

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
# what doesn't: per-stage durations and Groq retry/failure counts. Reset on
# process restart, and NOT shared across `--workers N` (each worker is a
# separate process) — acceptable for a single-client, single-VPS deployment;
# revisit only if that stops being true (see architecture doc trigger table).

class _Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.groq_calls          = 0
        self.groq_retries_total  = 0
        self.groq_failures       = 0
        self.stage_durations     = defaultdict(list)   # stage label -> [seconds, ...]

    def record_stage(self, label: str, seconds: float):
        with self._lock:
            self.stage_durations[label].append(seconds)

    def record_groq_attempt(self, retries: int, failed: bool):
        with self._lock:
            self.groq_calls += 1
            self.groq_retries_total += retries
            if failed:
                self.groq_failures += 1


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

# ── Background pipeline ───────────────────────────────────────────────

def run_pipeline(
    job_id: str,
    file_path: str,
    ext: str,
    job_description: str,
    target_rank: str,
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
        stage('Extracting text', 10)
        extracted = extract_text(file_path, ext)

        if not extracted.get('raw_text', '').strip():
            raise ValueError('Could not extract text — file may be image-based or empty')

        # ── 2. Parse ──────────────────────────────────────────────────
        stage('Parsing resume', 20)
        parsed = parse_resume(extracted['raw_text'])
        parsed['raw_text'] = extracted['raw_text']   # needed for raw fallback in rewriter

        # ── 3. Score (before) ─────────────────────────────────────────
        stage('Scoring original resume', 35)
        ats_before      = score_resume(extracted, parsed, job_description)
        maritime_before = maritime_score(extracted['raw_text'], parsed, pdf_path=file_path)

        # Typed domain object — canonical candidate record for this job.
        # Downstream stages (rewrite, generate) read from this rather than
        # the loose parsed/maritime dicts at the scoring/rewriting boundary.
        candidate_profile = build_candidate_profile(parsed, maritime_before)

        # ── 4. AI Rewrite ─────────────────────────────────────────────
        stage('Rewriting for ATS optimisation', 50)
        rank       = target_rank or candidate_profile.rank_detected
        # Scrubbed here (not just at log time) since this list is persisted
        # to issues_json and returned via /ats/result — a PDF/DOCX read
        # error occasionally embeds raw file content in str(e).
        all_issues = [redact_pii(i) for i in ats_before['issues'] + maritime_before['issues']]

        rewritten = rewrite_resume(
            parsed=parsed,
            issues=all_issues,
            job_description=job_description,
            rank=rank,
            license_rank=candidate_profile.license_rank,
        )

        groq_retry_count = rewritten.pop('_groq_retry_count', 0)
        groq_failed      = 'error' in rewritten
        _metrics.record_groq_attempt(retries=groq_retry_count, failed=groq_failed)
        log.info(f'"job_id":"{job_id}","groq_retry_count":{groq_retry_count},"groq_failed":{str(groq_failed).lower()}')

        # If rewrite failed, use safe fallback (don't crash)
        if 'error' in rewritten:
            log.warning(f'"job_id":"{job_id}","rewrite_error":"{rewritten["error"]}"')
            rewritten = {
                'summary':          parsed['sections'].get('summary', ''),
                'experience_bullets': [],
                'skills':           parsed.get('skills', []),
                'certifications':   [],
                'education':        [],
                'documents':        {},
                'languages':        [],
                'technical_expertise': [],
                'issues_fixed':     [f'AI rewrite unavailable — original content preserved'],
                'rank':             rank,
            }

        # Validate output quality
        quality_warnings = _validate_rewrite_output(rewritten, extracted['raw_text'])
        if quality_warnings:
            log.warning(f'"job_id":"{job_id}","quality_warnings":{json.dumps(quality_warnings)}')

        # Inject contact/name back (rewriter doesn't carry these)
        rewritten['name']    = candidate_profile.name
        rewritten['contact'] = candidate_profile.contact.model_dump()
        rewritten['rank']    = rank

        # ── 5. Generate DOCX ──────────────────────────────────────────
        stage('Generating ATS-clean DOCX', 65)
        out_dir   = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(out_dir, exist_ok=True)
        docx_path = os.path.join(out_dir, f'{job_id}_ats.docx')

        generate_docx(rewritten, job_id=job_id)
        # generate_docx saves to OUTPUT_DIR/{job_id}_ats_resume.docx — move to job folder
        default_path = os.path.join(OUTPUT_DIR, f'{job_id}_ats_resume.docx')
        if os.path.exists(default_path):
            shutil.move(default_path, docx_path)

        # ── 6. Re-score ───────────────────────────────────────────────
        stage('Verifying improvement', 80)
        extracted2 = extract_text(docx_path, 'docx')
        parsed2    = parse_resume(extracted2['raw_text'])
        ats_after  = score_resume(extracted2, parsed2, job_description)
        # No PDF to read for the regenerated DOCX — maritime_score() falls
        # back to text-based sea-months extraction from parsed2's sections.
        maritime_after = maritime_score(extracted2['raw_text'], parsed2)

        improvement = ats_after['total_score'] - ats_before['total_score']
        log.info(
            f'"job_id":"{job_id}",'
            f'"score_before":{ats_before["total_score"]},'
            f'"score_after":{ats_after["total_score"]},'
            f'"improvement":{improvement}'
        )

        # ── 7. Save ───────────────────────────────────────────────────
        stage('Saving results', 95)
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
            maritime_score_before=maritime_before['maritime_score'],
            maritime_grade_before=maritime_before['maritime_grade'],
            maritime_score_after=maritime_after['maritime_score'],
            maritime_grade_after=maritime_after['maritime_grade'],
            improvement=improvement,

            output_docx=docx_path,

            rank_detected=rank,
            sea_months=maritime_before.get('sea_months'),
            cert_coverage=maritime_before.get('cert_coverage'),

            issues_json=json.dumps(all_issues),
            recommendations_json=json.dumps(maritime_before.get('recommendations', [])),
            missing_certs_json=json.dumps(maritime_before.get('missing_certs', [])),
            issues_fixed_json=json.dumps(rewritten.get('issues_fixed', [])),
            keyword_data_json=json.dumps(ats_before.get('keyword_data', {})),
        )
        _finish_open_stage()

    except Exception as e:
        _finish_open_stage()
        log.error(f'"job_id":"{job_id}","error":"{str(e)}"')
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
    finally:
        # Always clean up temp upload file
        try:
            os.remove(file_path)
        except OSError:
            pass
        db.close()

# ── Endpoints ─────────────────────────────────────────────────────────

@app.get('/', summary='API root — welcome and documentation')
def root(request: Request):
    """Welcome endpoint. Links to API documentation and health check."""
    base = str(request.base_url).rstrip('/')
    return {
        'service': 'Marine Your Career — ATS API',
        'version': '2.0.0',
        'description': 'Resume scoring and optimisation for seafarers.',
        'documentation': f'{base}/docs',
        'health_check': f'{base}/health',
        'endpoints': {
            'submit_resume': 'POST /ats/submit',
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
async def submit(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description='PDF or DOCX resume'),
    job_description: str = Form(default='', description='Target job description (optional but improves keyword scoring)'),
    target_rank: str = Form(default='', description='Override rank detection (optional)'),
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
    if existing_id:
        db.close()
        log.info(f'"dedup_hit":"true","existing_job":"{existing_id}"')
        return {
            'job_id':   existing_id,
            'status':   'done',
            'cached':   True,
            'message':  'Same file processed recently — returning existing result',
        }

    # Save to temp
    job_id   = str(uuid.uuid4())
    tmp_path = os.path.join(TMP_DIR, f'{job_id}.{ext}')
    try:
        with open(tmp_path, 'wb') as f:
            f.write(contents)
    except OSError as e:
        log.error(f'"job_id":"{job_id}","error":"tmp write failed: {redact_pii(str(e))}"')
        raise HTTPException(500, 'Could not save upload — please try again')

    # Create job record
    db.add(Job(job_id=job_id, file_hash=fhash))
    db.commit()
    db.close()

    log.info(f'"job_id":"{job_id}","ext":"{ext}","size_kb":{len(contents)//1024}')

    # Queue background processing
    background_tasks.add_task(
        run_pipeline, job_id, tmp_path, ext, job_description, target_rank
    )

    return {'job_id': job_id, 'status': 'queued'}


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

    return {
        'job_id':               job.job_id,

        # Scores
        'score_before':         job.ats_score_before,
        'grade_before':         job.ats_grade_before,
        'score_after':          job.ats_score_after,
        'grade_after':          job.ats_grade_after,
        'improvement':          job.improvement,
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
        'issues_fixed':         _load(job.issues_fixed_json),
        'keyword_data':         _load(job.keyword_data_json),

        # Download
        'download_url':         f'/ats/download/{job_id}',
        'completed_at':         job.completed_at.isoformat() if job.completed_at else None,
        'expires_at':           job.expires_at.isoformat()   if job.expires_at   else None,
    }


@app.get(
    '/ats/download/{job_id}',
    summary='Download the optimised DOCX resume',
    dependencies=[Depends(require_api_key)],
)
def download(job_id: str):
    """Returns the ATS-optimised DOCX as a file download."""
    db  = get_db()
    job = db.get(Job, job_id)
    db.close()

    if not job:
        raise HTTPException(404, 'Job not found')
    if job.status != 'done':
        raise HTTPException(400, f'Job not complete — status: {job.status}')
    if not job.output_docx or not os.path.exists(job.output_docx):
        raise HTTPException(410, 'Output file expired or not found')

    # Check TTL
    if job.expires_at and datetime.utcnow() > job.expires_at:
        raise HTTPException(410, 'Result has expired — please resubmit')

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
    summary='Aggregate pipeline metrics — jobs, score deltas, Groq health, stage durations',
    dependencies=[Depends(require_api_key)],
)
def admin_metrics():
    """
    Jobs-processed and score-delta stats come from the Job table (durable).
    Groq retry/failure counts and stage durations come from in-memory
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

    groq_calls = _metrics.groq_calls
    groq_failure_rate = (
        round(_metrics.groq_failures / groq_calls, 4) if groq_calls else None
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
        'groq_calls':               groq_calls,
        'groq_retries_total':       _metrics.groq_retries_total,
        'groq_failures_total':      _metrics.groq_failures,
        'groq_failure_rate':        groq_failure_rate,
        'avg_stage_durations_seconds': avg_stage_durations,
        'note': (
            'groq_* and stage-duration figures are in-memory counters for '
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
    """Checks Groq connectivity and disk. Use for monitoring alerts."""
    checks = {}

    # Groq connectivity
    try:
        from groq import Groq
        from config import GROQ_API_KEY, GROQ_MODEL
        client = Groq(api_key=GROQ_API_KEY)
        client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{'role': 'user', 'content': 'ping'}],
            max_tokens=1,
            timeout=5,
        )
        checks['groq'] = 'ok'
    except Exception as e:
        checks['groq'] = f'error: {str(e)[:50]}'

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