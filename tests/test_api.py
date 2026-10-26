# tests/test_api.py
# Focused tests for the two real gaps found and fixed while auditing
# api.py's scalability/concurrency handling: (1) Base.metadata.create_all()
# never migrates an EXISTING database when the Job model gains a column —
# confirmed as a real, currently-live bug against the actual data/jobs.db
# file (missing 'maritime_grade_before', would have raised
# sqlite3.OperationalError on every real request) — and (2) nothing
# previously bounded how many resumes could be processing at once.
#
# Not chasing full endpoint coverage here (that would need mocking Claude,
# spaCy, and the whole pipeline) — these two are pure, isolated, and were
# the actual bugs found.

import os
import sqlite3
import uuid

import pytest


def test_ensure_schema_columns_repairs_a_genuinely_stale_database(tmp_path):
    """
    Real bug fixed, confirmed against the actual data/jobs.db: it was
    missing 'maritime_grade_before' entirely — a partial-migration
    artifact from when maritime scoring was split into separate
    before/after columns — because Base.metadata.create_all() only
    creates tables that don't exist yet, it never alters an existing
    one. Every query touching the Job table (every endpoint) would
    have raised sqlite3.OperationalError against the real production
    database. This builds a fresh, deliberately stale DB file (a Job
    table missing a real column the current model expects) and
    confirms _ensure_schema_columns() repairs it without touching
    existing data.
    """
    import api

    db_path = str(tmp_path / 'stale_jobs.db')
    conn = sqlite3.connect(db_path)
    # A stale schema: has job_id and status, but missing (among others)
    # maritime_grade_before -- mirrors the real confirmed gap.
    conn.execute(
        'CREATE TABLE jobs (job_id VARCHAR(36) PRIMARY KEY, status VARCHAR(20))'
    )
    conn.execute("INSERT INTO jobs (job_id, status) VALUES ('real-job-1', 'done')")
    conn.commit()
    conn.close()

    from sqlalchemy import create_engine, event
    test_engine = create_engine(f'sqlite:///{db_path}', connect_args={'check_same_thread': False})

    @event.listens_for(test_engine, 'connect')
    def _set_wal(dbapi_conn, _):
        dbapi_conn.execute('PRAGMA journal_mode=WAL')

    original_engine = api.engine
    api.engine = test_engine
    try:
        api._ensure_schema_columns()
    finally:
        api.engine = original_engine

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute('PRAGMA table_info(jobs)').fetchall()}
    # Every column the live ORM model expects must now be present.
    model_columns = {col.name for col in api.Job.__table__.columns}
    missing = model_columns - cols
    assert not missing, f'Columns still missing after repair: {missing}'

    # Existing data must survive untouched -- this is an additive
    # repair, never a destructive one.
    row = conn.execute("SELECT job_id, status FROM jobs WHERE job_id = 'real-job-1'").fetchone()
    assert row == ('real-job-1', 'done'), 'Existing row was lost or altered during schema repair'
    conn.close()


def test_ensure_schema_columns_is_a_no_op_on_an_already_correct_schema():
    """
    Happy path: running the repair against a schema that's already
    correct (the normal case on every subsequent app startup) must not
    raise, and must not need to touch anything.
    """
    import api
    # The real data/jobs.db, already repaired by this same function at
    # module import time -- a second run must be a clean no-op.
    api._ensure_schema_columns()


def _make_temp_jobs_db(tmp_path):
    """
    Real jobs.db schema (via the live ORM model) in an isolated temp
    file — _try_acquire_job_slot() talks to api.DB_PATH via its own raw
    sqlite3 connection, so tests point that at a throwaway file rather
    than the real data/jobs.db.
    """
    import api
    from sqlalchemy import create_engine

    db_path = str(tmp_path / 'test_jobs.db')
    engine = create_engine(f'sqlite:///{db_path}')
    api.Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def test_try_acquire_job_slot_blocks_at_the_configured_limit(tmp_path, monkeypatch):
    """
    Real gap fixed, confirmed by an actual `uvicorn --workers 2` load
    test against a real running server: the prior in-memory
    threading.Lock-guarded counter was per-worker-process, so
    MAX_CONCURRENT_JOBS=2 with 2 workers let 4 jobs run concurrently, not
    2 -- silently doubling the real resource-exhaustion ceiling the
    guard exists to enforce on a single small VPS. This backs the check
    with the real jobs.db instead (see _try_acquire_job_slot's
    docstring), so the limit is a property of the one DB every worker
    process shares, not of any one process's memory.
    """
    import api

    db_path = _make_temp_jobs_db(tmp_path)
    monkeypatch.setattr(api, 'DB_PATH', db_path)
    monkeypatch.setattr(api, 'MAX_CONCURRENT_JOBS', 3)

    results = [api._try_acquire_job_slot(str(uuid.uuid4()), f'hash-{i}') for i in range(5)]
    assert results == [True, True, True, False, False], (
        f'Expected exactly 3 successful acquires against a limit of 3, got: {results}'
    )

    conn = sqlite3.connect(db_path)
    queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
    conn.close()
    assert queued == 3, f'Expected exactly 3 real queued rows in jobs.db, found {queued}'


def test_try_acquire_job_slot_frees_up_once_a_job_leaves_queued_or_processing(tmp_path, monkeypatch):
    """
    There's no separate release() call anymore (see
    _try_acquire_job_slot's docstring) -- a slot frees up purely because
    the row's own status moves out of queued/processing, via the exact
    same update_job() call run_pipeline already makes on completion.
    """
    import api

    db_path = _make_temp_jobs_db(tmp_path)
    monkeypatch.setattr(api, 'DB_PATH', db_path)
    monkeypatch.setattr(api, 'MAX_CONCURRENT_JOBS', 1)

    held_job_id = str(uuid.uuid4())
    assert api._try_acquire_job_slot(held_job_id, 'hash-a') is True
    assert api._try_acquire_job_slot(str(uuid.uuid4()), 'hash-b') is False, (
        'Expected the single slot to already be held'
    )

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE jobs SET status = 'done' WHERE job_id = ?", (held_job_id,))
    conn.commit()
    conn.close()

    assert api._try_acquire_job_slot(str(uuid.uuid4()), 'hash-c') is True, (
        'Expected a fresh slot once the held job left queued/processing'
    )


def test_try_acquire_job_slot_is_race_free_under_real_concurrent_sqlite_connections(tmp_path, monkeypatch):
    """
    Direct analog of the old in-memory guard's thread-safety test, but
    now exercising real SQLite locking (BEGIN IMMEDIATE) instead of a
    Python threading.Lock -- fires many threads, each opening its OWN
    real sqlite3 connection to the same file at genuinely the same
    moment, and confirms the total successful acquires never exceeds the
    configured limit despite the real cross-connection race.
    """
    import threading
    import api

    db_path = _make_temp_jobs_db(tmp_path)
    monkeypatch.setattr(api, 'DB_PATH', db_path)
    monkeypatch.setattr(api, 'MAX_CONCURRENT_JOBS', 5)

    successes = []
    lock = threading.Lock()

    def worker(i):
        ok = api._try_acquire_job_slot(str(uuid.uuid4()), f'hash-{i}')
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(successes) == 5, (
        f'Expected exactly 5 successful acquires under real concurrent contention, got {sum(successes)}'
    )

    conn = sqlite3.connect(db_path)
    queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
    conn.close()
    assert queued == 5, f'Expected exactly 5 real queued rows after the race, found {queued}'


def test_update_job_survives_many_genuinely_simultaneous_writes_under_wal(tmp_path, monkeypatch):
    """
    Deliberate crash-resilience audit item: run_pipeline's update_job()
    calls (marking jobs done/failed at each stage) can genuinely overlap
    across concurrent jobs -- BackgroundTasks runs each job on its own
    thread. Confirms real, literally-simultaneous writes (forced via a
    threading.Barrier so every thread hits the DB at the same instant,
    not just "started close together") to DIFFERENT rows never surface
    a raw sqlite3 "database is locked" error to the caller. WAL mode
    plus pysqlite's default 5000ms busy_timeout (confirmed present on
    this engine via PRAGMA busy_timeout) is what makes this safe --
    concurrent writers serialize and retry rather than failing outright.
    Uses 60 threads, well beyond this service's real concurrency ceiling
    (MAX_CONCURRENT_JOBS, even doubled across 2 workers), to leave a
    genuine margin rather than just barely covering today's config.
    """
    import threading
    import api
    from sqlalchemy import create_engine, event

    db_path = str(tmp_path / 'concurrent_test.db')
    test_engine = create_engine(f'sqlite:///{db_path}', connect_args={'check_same_thread': False})

    @event.listens_for(test_engine, 'connect')
    def _set_wal(dbapi_conn, _):
        dbapi_conn.execute('PRAGMA journal_mode=WAL')

    api.Base.metadata.create_all(test_engine)
    monkeypatch.setattr(api, 'engine', test_engine)

    n = 60
    job_ids = [str(uuid.uuid4()) for _ in range(n)]
    db = api.get_db()
    for jid in job_ids:
        db.add(api.Job(job_id=jid, status='processing'))
    db.commit()
    db.close()

    errors    = []
    successes = []
    lock      = threading.Lock()
    barrier   = threading.Barrier(n)

    def worker(job_id):
        barrier.wait()
        try:
            db = api.get_db()
            api.update_job(db, job_id, status='done', progress=100)
            db.close()
            with lock:
                successes.append(job_id)
        except Exception as e:
            with lock:
                errors.append((type(e).__name__, str(e)))

    threads = [threading.Thread(target=worker, args=(jid,)) for jid in job_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f'Expected zero errors from genuinely concurrent writes, got: {errors}'
    assert len(successes) == n

    db = api.get_db()
    done_count = db.query(api.Job).filter(api.Job.status == 'done').count()
    db.close()
    assert done_count == n, f'Expected all {n} rows to be persisted as done, found {done_count}'


def test_rate_limiter_returns_429_after_the_configured_threshold(tmp_path, monkeypatch):
    """
    Real, previously-untested code path: the rate-limiting commit that
    added this was syntax-checked only (fastapi/slowapi weren't
    installed in that environment). Actually exercises /ats/submit via
    TestClient and confirms the 11th request from the same client
    within the window is rejected with 429, not silently accepted.

    Fakes out BackgroundTasks.add_task (same technique as
    test_submit_never_uses_the_raw_filename_for_the_saved_path) so
    run_pipeline never actually executes. Real bug fixed in THIS test,
    confirmed by a real failure: without this, each request pays the
    real ~6-13s extract_pdf() subprocess-spawn cost (see
    _extract_with_timeout in api.py) even for a file that fails
    extraction outright -- 12 such requests span more than slowapi's
    60-second rolling window in real wall-clock time, so by the 11th
    request the 1st request's slot has already rolled off and the
    limiter correctly does NOT fire. That's a real, platform-specific
    (macOS 'spawn' vs Linux 'fork' -- see _extract_with_timeout's
    docstring) timing artifact of the pipeline this test doesn't need to
    run at all -- it exists to test the RATE LIMITER, not extraction.

    Also isolated to a temp DB_PATH (same _make_temp_jobs_db pattern as
    the _try_acquire_job_slot tests above), rather than the real
    data/jobs.db -- real bug fixed here too: with add_task mocked out,
    every one of these 12 submissions that clears _try_acquire_job_slot
    still inserts a real 'queued' row (that's what acquiring the slot
    IS), and since run_pipeline never runs to move it to done/failed,
    NOTHING ever frees that slot. Confirmed as a real, live problem: a
    prior version of this test (touching the real DB_PATH) left 5 rows
    permanently stuck 'queued' after every test run, which then made the
    very next test that hit the real /ats/submit endpoint fail with a
    genuine 503 "at capacity" -- MAX_CONCURRENT_JOBS's default (5) had
    silently been fully consumed by a previous test's leftovers.
    """
    pytest.importorskip('slowapi')
    from fastapi import BackgroundTasks
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(BackgroundTasks, 'add_task', lambda self, func, *a, **kw: None)
    monkeypatch.setattr(api, 'DB_PATH', _make_temp_jobs_db(tmp_path))

    os.environ.setdefault('MYC_API_KEY', 'test-key-for-rate-limit-test')
    api.API_KEY = os.environ['MYC_API_KEY']
    api.limiter.reset()

    client = TestClient(api.app)
    fake_pdf = b'%PDF-1.4\n' + b'x' * 200

    statuses = []
    for i in range(12):
        r = client.post(
            '/ats/submit',
            headers={'X-API-Key': api.API_KEY},
            files={'file': (f'rate_limit_test_{i}.pdf', fake_pdf, 'application/pdf')},
        )
        statuses.append(r.status_code)

    assert statuses[:10].count(429) == 0, f'Expected the first 10 requests to succeed, got: {statuses}'
    assert 429 in statuses[10:], f'Expected the 11th+ request to be rate-limited, got: {statuses}'


def test_validate_file_rejects_a_file_over_max_file_mb():
    """
    Deliberate exception-handling audit item: a file 1 byte over
    MAX_FILE_MB must be rejected with a clear, specific 413 before any
    processing/queuing occurs -- not accepted, not a generic 500, not
    silently truncated. Tests _validate_file() directly rather than via
    TestClient/the full /ats/submit endpoint, since the module-level
    rate limiter's in-memory state persists across tests in the same
    process (see test_rate_limiter_returns_429_after_the_configured_
    threshold above, which already exhausts the same TestClient's
    per-IP quota) -- this keeps the test isolated to the actual
    validation logic being audited.
    """
    import api
    from fastapi import HTTPException

    oversized = b'%PDF-1.4\n' + b'x' * (api.MAX_FILE_BYTES + 1)
    with pytest.raises(HTTPException) as exc_info:
        api._validate_file(oversized, 'oversized.pdf')
    assert exc_info.value.status_code == 413
    assert 'MB limit' in exc_info.value.detail


def test_run_pipeline_error_message_distinguishes_extraction_failure_reason(tmp_path):
    """
    Real gap fixed, confirmed by deliberately auditing exception handling
    against real corrupt/password-protected/zero-byte files: run_pipeline
    discarded extract_pdf()'s specific structural_issues detail (e.g.
    PASSWORD_PROTECTED_PDF, or a real PDF_READ_ERROR message) and always
    raised the same generic "file may be image-based or empty" message --
    even for a password-protected upload, which needs completely
    different, actionable advice (remove the password) than a genuinely
    corrupt file (re-export and re-upload). The specific reason must now
    reach job.error_detail.
    """
    import uuid
    import api

    path = tmp_path / 'password_protected.pdf'
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), 'test')
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw='secret', owner_pw='ownersecret')
    doc.close()

    job_id = str(uuid.uuid4())
    tmp_path_str = str(tmp_path / f'{job_id}.pdf')
    import shutil
    shutil.copy(str(path), tmp_path_str)

    db = api.get_db()
    db.add(api.Job(job_id=job_id, file_hash=f'test-{job_id}'))
    db.commit()
    db.close()

    api.run_pipeline(job_id, tmp_path_str, 'pdf', '', '')

    db = api.get_db()
    job = db.get(api.Job, job_id)
    db.close()

    assert job.status == 'failed'
    assert 'PASSWORD_PROTECTED_PDF' in job.error_detail, (
        f'Expected the specific failure reason in error_detail, got: {job.error_detail!r}'
    )


def test_run_pipeline_falls_back_gracefully_on_total_claude_outage(monkeypatch):
    """
    Simulates a total Claude outage (client is None). The job must still
    complete ('done', not 'failed') with the original content preserved.
    """
    import uuid
    import api
    import services.ai_client as ai_client

    monkeypatch.setattr(ai_client, 'ANTHROPIC_API_KEY', 'sk-invalid-fake-key-for-outage-test')
    monkeypatch.setattr(ai_client, 'client', None)

    real_resume = None
    for candidate in [
        '/Users/shreya/Desktop/marine-ats/data/myc_resume/1. 2024 Candidates/Gilfred Chettiar/GilfredChettiarResume.pdf',
    ]:
        if os.path.exists(candidate):
            real_resume = candidate
            break
    if real_resume is None:
        import pytest
        pytest.skip('Real candidate fixture not present in this environment')

    job_id = str(uuid.uuid4())
    tmp_path_str = f'/tmp/{job_id}.pdf'
    import shutil
    shutil.copy(real_resume, tmp_path_str)

    db = api.get_db()
    db.add(api.Job(job_id=job_id, file_hash=f'test-claude-outage-{job_id}'))
    db.commit()
    db.close()

    api.run_pipeline(job_id, tmp_path_str, 'pdf', '', '')

    db = api.get_db()
    job = db.get(api.Job, job_id)
    db.close()

    assert job.status == 'done', f'Expected the job to complete despite the Claude outage, got: {job.status}'
    assert job.output_docx and os.path.exists(job.output_docx)


def test_submit_never_uses_the_raw_filename_for_the_saved_path(monkeypatch):
    """
    Security audit item: a crafted filename like "../../../../etc/passwd.pdf"
    must never let an upload escape TMP_DIR. _validate_file() only ever
    extracts a file *extension* from the filename via .rsplit('.', 1) --
    the saved path itself is built from a fresh, server-generated
    uuid4() job_id, never from any part of the attacker-controlled
    filename. Proven here end-to-end through the real /ats/submit
    endpoint (not just by reading the validation code): fakes out
    BackgroundTasks.add_task to capture exactly what path run_pipeline
    would have been called with, without needing the full pipeline
    (spaCy/Claude) to actually run.
    """
    import api
    from fastapi import BackgroundTasks
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, 'API_KEY', 'traversal-test-key')
    api.limiter.reset()

    captured = {}

    def fake_add_task(self, func, *args, **kwargs):
        captured['job_id']    = args[0]
        captured['file_path'] = args[1]

    monkeypatch.setattr(BackgroundTasks, 'add_task', fake_add_task)

    client  = TestClient(api.app)
    content = b'%PDF-1.4\n' + b'traversal test content ' * 20

    r = client.post(
        '/ats/submit',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('../../../../etc/passwd.pdf', content, 'application/pdf')},
    )

    assert r.status_code == 200, r.text
    saved_path = captured['file_path']
    job_id     = captured['job_id']

    assert saved_path == os.path.join(api.TMP_DIR, f'{job_id}.pdf'), (
        f'Expected the saved path to be built purely from the server-generated job_id, got: {saved_path}'
    )
    assert os.path.exists(saved_path), 'Expected the file to have actually been written to the safe, uuid-based path'
    assert 'etc' not in saved_path and 'passwd' not in saved_path, (
        f'Attacker-controlled filename leaked into the real saved path: {saved_path}'
    )

    os.remove(saved_path)  # run_pipeline never actually ran (faked out above), so nothing else cleans this up
    conn = sqlite3.connect(api.DB_PATH)
    conn.execute('DELETE FROM jobs WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()


def test_submit_rejects_exe_content_disguised_as_pdf_or_docx(monkeypatch):
    """
    Security audit item: a real Windows PE executable (MZ header) renamed
    to .pdf or .docx must be rejected by the magic-byte check, not just
    waved through on extension alone.
    """
    import api
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, 'API_KEY', 'malcontent-test-key')
    api.limiter.reset()
    client = TestClient(api.app)

    # A real minimal Windows PE header (MZ magic bytes).
    exe_content = b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00' + b'\x00' * 100

    r_pdf = client.post(
        '/ats/submit', headers={'X-API-Key': api.API_KEY},
        files={'file': ('malware.pdf', exe_content, 'application/pdf')},
    )
    assert r_pdf.status_code == 415, (
        f'Expected an EXE disguised as .pdf to be rejected by the magic-byte check, got {r_pdf.status_code}: {r_pdf.text}'
    )

    r_docx = client.post(
        '/ats/submit', headers={'X-API-Key': api.API_KEY},
        files={'file': ('malware.docx', exe_content,
                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
    )
    assert r_docx.status_code == 415, (
        f'Expected an EXE disguised as .docx to be rejected by the magic-byte check, got {r_docx.status_code}: {r_docx.text}'
    )


def test_submit_accepts_a_non_ooxml_zip_disguised_as_docx_but_fails_cleanly_downstream(monkeypatch):
    """
    Real, confirmed gap: _validate_file()'s DOCX check only verifies the
    first two bytes are 'PK' -- the generic ZIP signature shared by
    DOCX/XLSX/JAR/APK/any ZIP archive -- not that the archive is actually
    a valid OOXML/DOCX package. A plain ZIP (not a real DOCX) renamed to
    .docx passes this shallow check and gets queued.

    This is confirmed here NOT to be a crash risk, though:
    extract_docx() (pipeline/extractor.py) wraps python-docx's
    Document() call in a broad try/except and returns a clean
    DOCX_READ_ERROR structural issue for anything that isn't a real
    OOXML package. Proven end-to-end: the job is accepted at submit
    time, then fails cleanly (not silently, not a crash) once the
    pipeline actually tries to read it.
    """
    import io
    import zipfile
    import api
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, 'API_KEY', 'ziptest-key')
    api.limiter.reset()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('not_a_docx.txt', 'this is a plain zip, not a real OOXML/DOCX package')
    zip_bytes = buf.getvalue()
    assert zip_bytes[:2] == b'PK'  # confirms this really would pass the current magic-byte check

    client = TestClient(api.app)
    r = client.post(
        '/ats/submit', headers={'X-API-Key': api.API_KEY},
        files={'file': ('fake.docx', zip_bytes,
                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
    )
    assert r.status_code == 200, (
        f'Confirms the gap: this non-OOXML zip should be ACCEPTED by the weak magic-byte '
        f'check (proving it is not real content validation), got {r.status_code}: {r.text}'
    )
    job_id = r.json()['job_id']

    status_resp = client.get(f'/ats/status/{job_id}', headers={'X-API-Key': api.API_KEY})
    assert status_resp.json()['status'] == 'failed', (
        f'Expected the pipeline to fail cleanly once it actually tries to read the fake DOCX, got: {status_resp.json()}'
    )

    result_resp = client.get(f'/ats/result/{job_id}', headers={'X-API-Key': api.API_KEY})
    assert 'DOCX_READ_ERROR' in (result_resp.json().get('detail') or ''), (
        f'Expected a clean DOCX_READ_ERROR in the failure detail, got: {result_resp.json()}'
    )


def test_admin_metrics_rejects_the_client_facing_key_when_a_distinct_admin_key_is_set(monkeypatch):
    """
    Built and reasoned about (require_admin_key() falls back to
    MYC_API_KEY only when MYC_ADMIN_KEY is unset) but never actually
    exercised end-to-end with two real, distinct keys until now.
    """
    import api
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, 'API_KEY', 'client-facing-key-abc')
    monkeypatch.setattr(api, 'ADMIN_API_KEY', 'admin-only-key-xyz')

    client = TestClient(api.app)

    r_client_key = client.get('/ats/admin/metrics', headers={'X-API-Key': api.API_KEY})
    assert r_client_key.status_code == 403, (
        f'Expected the client-facing key to be REJECTED for /ats/admin/metrics once a '
        f'distinct MYC_ADMIN_KEY is set, got {r_client_key.status_code}: {r_client_key.text}'
    )

    r_admin_key = client.get('/ats/admin/metrics', headers={'X-API-Key': api.ADMIN_API_KEY})
    assert r_admin_key.status_code == 200, (
        f'Expected the real admin key to be accepted, got {r_admin_key.status_code}: {r_admin_key.text}'
    )


def test_result_reports_whether_ai_rewrite_ran(monkeypatch, tmp_path):
    import json
    import api
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    test_engine = create_engine(f'sqlite:///{tmp_path / "rewrite_status.db"}')
    api.Base.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add_all([
            api.Job(
                job_id='rewrite-success',
                status='done',
                issues_fixed_json=json.dumps(['Improved summary']),
            ),
            api.Job(
                job_id='rewrite-fallback',
                status='done',
                issues_fixed_json=json.dumps([api.AI_REWRITE_FALLBACK_ISSUE]),
            ),
        ])
        db.commit()

    monkeypatch.setattr(api, 'API_KEY', 'rewrite-status-test-key')
    monkeypatch.setattr(api, 'get_db', lambda: Session(test_engine))
    api.limiter.reset()
    client = TestClient(api.app)

    success = client.get(
        '/ats/result/rewrite-success',
        headers={'X-API-Key': api.API_KEY},
    ).json()
    fallback = client.get(
        '/ats/result/rewrite-fallback',
        headers={'X-API-Key': api.API_KEY},
    ).json()

    assert success['ai_rewrite'] is True
    assert success['note'] is None
    assert fallback['ai_rewrite'] is False
    assert 'original resume content was preserved' in fallback['note']


def _build_zlib_bomb_pdf(decompressed_mb: int) -> bytes:
    """A minimal single-page PDF whose one content stream decompresses to
    `decompressed_mb` MB of zeros from a tiny on-disk FlateDecode payload."""
    import zlib

    raw        = b'\x00' * (decompressed_mb * 1024 * 1024)
    compressed = zlib.compress(raw, 9)

    objs = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << >> >>',
        b'<< /Length %d /Filter /FlateDecode >>\nstream\n' % len(compressed) + compressed + b'\nendstream',
    ]
    out     = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref_offset = len(out)
    n = len(objs) + 1
    out += f'xref\n0 {n}\n'.encode() + b'0000000000 65535 f \n'
    for off in offsets:
        out += f'{off:010d} 00000 n \n'.encode()
    out += f'trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF'.encode()
    return bytes(out)


def test_extract_with_timeout_kills_a_hanging_decompression_bomb(tmp_path, monkeypatch):
    """
    Real gap fixed, confirmed by a direct test against the real
    extraction code: extract_pdf() previously had no wall-clock bound at
    all -- a real 800MB zlib decompression bomb (built from a <1MB PDF,
    comfortably under MAX_FILE_MB) hung it for 90+ real seconds at ~100%
    CPU with zero sign of completing, and nothing stopped it. Uses a
    smaller bomb + a short timeout here to keep the test itself fast
    while still genuinely exercising the hang-then-kill path -- the
    important assertions are that the call returns close to the
    configured timeout (not after the bomb's real, much longer hang
    duration) and reports a clear EXTRACTION_TIMEOUT signal.
    """
    import time
    import api

    bomb_path = tmp_path / 'decompression_bomb.pdf'
    bomb_path.write_bytes(_build_zlib_bomb_pdf(decompressed_mb=300))

    monkeypatch.setattr(api, 'EXTRACTION_TIMEOUT_SECONDS', 3)

    t0     = time.monotonic()
    result = api._extract_with_timeout(str(bomb_path), 'pdf')
    elapsed = time.monotonic() - t0

    assert elapsed < 15, (
        f'Expected the hard kill to bound this well under 15s (timeout was 3s), took {elapsed:.1f}s -- '
        'the process may not actually have been killed'
    )
    assert result['raw_text'] == ''
    assert 'EXTRACTION_TIMEOUT' in result['structural_issues'][0], (
        f'Expected a clear EXTRACTION_TIMEOUT signal, got: {result["structural_issues"]}'
    )


def test_extract_with_timeout_does_not_false_positive_on_a_large_but_legitimate_result(tmp_path):
    """
    Real bug fixed in this function itself, confirmed by a direct test:
    an earlier version called proc.join(timeout) BEFORE ever reading the
    result multiprocessing.Queue. multiprocessing.Queue is pipe-backed
    with a limited OS buffer (~64KB) -- a child that has put() a larger
    payload blocks writing the remainder until something reads it, and
    join()-before-get() never does. A genuinely benign 975-page PDF
    (~65KB of real extracted text, no malicious structure at all) was
    being wrongly killed and reported as EXTRACTION_TIMEOUT purely
    because its correct, successful result was too big for the pipe --
    even though extract_pdf() completes that exact file in ~1.3s when
    called directly. Confirms the fixed version (reading the queue WITH
    the timeout, which drains the pipe as the child writes) returns the
    real, complete result instead of a false-positive timeout.
    """
    import pymupdf
    import api

    doc = pymupdf.open()
    for i in range(1000):
        page = doc.new_page()
        page.insert_text((72, 72), f'Page {i} - filler resume-like content for stress test.')
    path = tmp_path / 'large_legitimate.pdf'
    doc.save(str(path))
    doc.close()

    result = api._extract_with_timeout(str(path), 'pdf')

    assert 'EXTRACTION_TIMEOUT' not in str(result.get('structural_issues')), (
        f'A large but legitimate result was wrongly killed as a timeout: {result["structural_issues"]}'
    )
    assert len(result['raw_text']) > 60_000, (
        f'Expected the full, real extracted text (well over the ~64KB pipe buffer), got {len(result["raw_text"])} chars'
    )


def test_extract_with_timeout_still_works_normally_for_a_real_resume():
    """
    The timeout wrapper must not break the normal, fast path -- a real
    resume should extract quickly and correctly through it, same as
    calling extract_text() directly.
    """
    import api

    real_pdf = 'data/myc_resume/Srinivas Menon/SrinivasMenonResume.pdf'
    if not os.path.exists(real_pdf):
        import pytest
        pytest.skip('Real candidate fixture not present in this environment')

    result = api._extract_with_timeout(real_pdf, 'pdf')
    assert result['raw_text'], 'Expected real, non-empty extracted text for a real resume'
    assert 'EXTRACTION_TIMEOUT' not in result.get('structural_issues', [])


# ── /ats/score — scoring only, no Claude dependency ─────────────────────

def _real_pdf_fixture():
    real_pdf = 'data/myc_resume/Srinivas Menon/SrinivasMenonResume.pdf'
    if not os.path.exists(real_pdf):
        pytest.skip('Real candidate fixture not present in this environment')
    return real_pdf


def test_score_endpoint_matches_the_real_submit_pipelines_stage_3_and_4(monkeypatch):
    """
    The actual "no drift between the two code paths" guarantee: calls
    /ats/score via TestClient, then separately calls the exact same
    Stage 1-4 functions run_pipeline() itself calls for its "before"
    scores (extract_text -> parse_resume -> score_resume ->
    maritime_score) directly against the same real file, and asserts
    the numbers are identical. This is a real behavioral check, not
    just a code-reading claim that they share functions -- a future
    edit to either call site that quietly diverges (different
    argument, different stage order) would fail this test even if both
    still "looked like" they called the same functions.
    """
    import api
    from pipeline.extractor import extract_text
    from pipeline.parser import parse_resume
    from services.two_layer import score_layers
    from fastapi.testclient import TestClient

    real_pdf = _real_pdf_fixture()
    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    api.limiter.reset()

    with open(real_pdf, 'rb') as f:
        content = f.read()

    client = TestClient(api.app)
    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('resume.pdf', content, 'application/pdf')},
    )
    assert r.status_code == 200, r.text
    via_endpoint = r.json()

    # The same Stage 1-4 call sequence run_pipeline() makes, invoked
    # directly against the exact same bytes.
    extracted        = extract_text(real_pdf, 'pdf')
    parsed           = parse_resume(extracted['raw_text'])
    parsed['raw_text'] = extracted['raw_text']
    layers = score_layers(extracted, parsed, pdf_path=real_pdf, ext='pdf')
    ats_result = layers['generic']
    maritime_result = layers['maritime']

    assert via_endpoint['ats_score'] == ats_result['total_score']
    assert via_endpoint['ats_grade'] == ats_result['grade']
    if maritime_result:
        assert via_endpoint['maritime_score'] == maritime_result['maritime_score']
        assert via_endpoint['maritime_grade'] == maritime_result['maritime_grade']
        assert via_endpoint['rank_detected'] == maritime_result['rank_detected']
        assert via_endpoint['sea_months'] == maritime_result['sea_months']
        assert via_endpoint['cert_coverage'] == maritime_result['cert_coverage']
    else:
        assert via_endpoint['maritime_score'] is None


def test_score_endpoint_requires_api_key():
    from fastapi.testclient import TestClient
    import api

    client = TestClient(api.app)
    r = client.post('/ats/score', files={'file': ('x.pdf', b'%PDF-1.4\ntest', 'application/pdf')})
    assert r.status_code == 403


def test_score_endpoint_rejects_oversized_file(monkeypatch):
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    api.limiter.reset()
    client = TestClient(api.app)

    oversized = b'%PDF-1.4\n' + b'x' * (api.MAX_FILE_BYTES + 1)
    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('big.pdf', oversized, 'application/pdf')},
    )
    assert r.status_code == 413


def test_score_endpoint_rejects_exe_content_disguised_as_pdf(monkeypatch):
    """Same magic-byte validation as /ats/submit -- see _validate_file()."""
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    api.limiter.reset()
    client = TestClient(api.app)

    exe_content = b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00' + b'\x00' * 100
    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('malware.pdf', exe_content, 'application/pdf')},
    )
    assert r.status_code == 415


def test_score_endpoint_is_rate_limited_the_same_as_submit(monkeypatch):
    """
    Same 10/minute-per-IP limit as /ats/submit. Stub scoring so all
    requests run inside the limiter's 60-second window on every platform.
    """
    pytest.importorskip('slowapi')
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import api

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    monkeypatch.setattr(api, '_extract_with_timeout', lambda _path, _ext: {'raw_text': 'resume'})
    monkeypatch.setattr(api, 'parse_resume', lambda raw_text: {'raw_text': raw_text, 'rank': '', 'sections': {}, 'skills': [], 'contact': {}})
    monkeypatch.setattr(
        api,
        'score_layers',
        lambda *_args, **_kwargs: {
            'generic': {'total_score': 0, 'grade': 'FAIL', 'issues': [], 'keyword_data': {}},
            'maritime': None,
            'overlay': False,
            'job_description': '',
            'role': '',
        },
    )
    monkeypatch.setattr(
        api,
        'build_candidate_profile',
        lambda *_args: SimpleNamespace(name='', rank_detected='', sea_months=0),
    )
    api.limiter.reset()
    client = TestClient(api.app)

    statuses = []
    for i in range(12):
        r = client.post(
            '/ats/score',
            headers={'X-API-Key': api.API_KEY},
            files={'file': (f'resume_{i}.pdf', b'%PDF-1.4 test', 'application/pdf')},
        )
        statuses.append(r.status_code)

    assert statuses[:10].count(429) == 0, f'Expected the first 10 requests to succeed, got: {statuses}'
    assert 429 in statuses[10:], f'Expected the 11th+ request to be rate-limited, got: {statuses}'


def test_score_endpoint_does_not_persist_a_job_row(monkeypatch, tmp_path):
    """
    /ats/score is read-only scoring, not a submission -- confirms it
    never writes to the Job table at all (no job_id in the response,
    and the jobs table row count is unchanged after a call).
    """
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    monkeypatch.setattr(api, 'DB_PATH', _make_temp_jobs_db(tmp_path))
    api.limiter.reset()
    client = TestClient(api.app)

    real_pdf = _real_pdf_fixture()
    with open(real_pdf, 'rb') as f:
        content = f.read()

    conn = sqlite3.connect(api.DB_PATH)
    before = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    conn.close()

    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('resume.pdf', content, 'application/pdf')},
    )
    assert r.status_code == 200
    assert 'job_id' not in r.json()

    conn = sqlite3.connect(api.DB_PATH)
    after = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    conn.close()
    assert after == before, 'Expected /ats/score to never write a Job row'


def test_root_endpoint_lists_every_real_candidate_facing_route():
    """
    Real gap fixed, caught by actually hitting the real running server's
    root endpoint (not assumed from reading the route decorators): the
    root() welcome response's own hardcoded 'endpoints' dict is a
    SEPARATE, manually-maintained listing from the actual registered
    FastAPI routes -- adding /ats/score as a real, working route didn't
    automatically add it here, so a real caller checking `GET /` (a
    reasonable first thing to do, e.g. to confirm the service is up and
    see what's available) got back a listing that silently omitted a
    real, working endpoint. Confirms every real POST/GET/DELETE `/ats/*`
    route MYC's app is actually meant to call is present -- deliberately
    NOT asserting the admin-only /ats/admin/metrics route is listed here
    too, since that one's intentionally left out of the public welcome
    response.
    """
    from fastapi.testclient import TestClient
    import api

    client = TestClient(api.app)
    r = client.get('/')
    assert r.status_code == 200
    listed = ' '.join(r.json()['endpoints'].values())

    for real_route in [
        'POST /ats/submit', 'POST /ats/score', 'GET /ats/status/{job_id}',
        'GET /ats/result/{job_id}', 'GET /ats/download/{job_id}',
        'DELETE /ats/job/{job_id}', 'POST /ats/cleanup',
    ]:
        assert real_route in listed, f'Expected {real_route!r} to be listed at GET /, got: {r.json()["endpoints"]}'


def test_dashboard_is_served_by_the_api():
    import asyncio
    import httpx
    import api

    async def request_dashboard():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            return await client.get('/dashboard')

    response = asyncio.run(request_dashboard())
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert 'The MYC API key stays on the server' in response.text
    assert 'Dashboard access key' in response.text
    assert 'apiKeyInput' not in response.text


def test_dashboard_session_authenticates_remote_tunnel_user(monkeypatch):
    import asyncio
    import httpx
    import api

    monkeypatch.setattr(api, 'API_KEY', 'server-api-key')
    monkeypatch.setattr(api, 'DASHBOARD_ACCESS_KEY', 'separate-team-dashboard-key')

    async def exercise_remote_session():
        transport = httpx.ASGITransport(app=api.app, client=('203.0.113.18', 12345))
        async with httpx.AsyncClient(
            transport=transport,
            base_url='https://marine-test.ngrok-free.app',
        ) as client:
            dashboard = await client.get('/dashboard')
            session_status = await client.get('/dashboard/session')
            login = await client.post(
                '/dashboard/session',
                headers={'Origin': 'https://marine-test.ngrok-free.app'},
                json={'access_key': 'separate-team-dashboard-key'},
            )
            proxy = await client.get(
                '/dashboard-api/ats/status/00000000-0000-0000-0000-000000000000',
                headers={'Origin': 'https://marine-test.ngrok-free.app'},
            )
            return dashboard, session_status, login, proxy

    dashboard, session_status, login, proxy = asyncio.run(exercise_remote_session())
    assert dashboard.status_code == 200
    assert session_status.json() == {'required': True, 'authenticated': False}
    assert login.status_code == 200
    assert login.json() == {'authenticated': True}
    cookie = login.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'secure' in cookie and 'samesite=strict' in cookie
    assert proxy.status_code == 404
    assert proxy.json()['detail'] == 'Job not found'


def test_dashboard_remote_login_rejects_invalid_key_and_cross_origin(monkeypatch):
    import asyncio
    import httpx
    import api

    monkeypatch.setattr(api, 'DASHBOARD_ACCESS_KEY', 'separate-team-dashboard-key')

    async def attempt_login(key, origin):
        transport = httpx.ASGITransport(app=api.app, client=('203.0.113.18', 12345))
        async with httpx.AsyncClient(
            transport=transport,
            base_url='https://marine-test.ngrok-free.app',
        ) as client:
            return await client.post(
                '/dashboard/session',
                headers={'Origin': origin},
                json={'access_key': key},
            )

    invalid = asyncio.run(attempt_login('wrong-key', 'https://marine-test.ngrok-free.app'))
    cross_origin = asyncio.run(attempt_login('separate-team-dashboard-key', 'https://attacker.example'))
    assert invalid.status_code == 403
    assert cross_origin.status_code == 403


def test_dashboard_local_access_does_not_require_a_session(monkeypatch):
    import asyncio
    import httpx
    import api

    monkeypatch.setattr(api, 'DASHBOARD_ACCESS_KEY', '')

    async def request_dashboard():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            response = await client.get('/dashboard/session')
            return response

    response = asyncio.run(request_dashboard())
    assert response.status_code == 200
    assert response.json() == {'required': False, 'authenticated': True}


def test_dashboard_and_proxy_are_not_available_remotely(monkeypatch):
    import asyncio
    import httpx
    import api

    monkeypatch.setattr(api, 'API_KEY', 'dashboard-proxy-test-key')
    monkeypatch.setattr(api, 'DASHBOARD_ACCESS_KEY', '')

    async def request_remote_dashboard():
        transport = httpx.ASGITransport(app=api.app, client=('198.51.100.5', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            dashboard = await client.get('/dashboard')
            proxy = await client.get(
                '/dashboard-api/ats/status/00000000-0000-0000-0000-000000000000',
            )
            return dashboard, proxy

    dashboard, proxy = asyncio.run(request_remote_dashboard())
    assert dashboard.status_code == 403
    assert proxy.status_code == 403

    async def request_with_untrusted_host():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://attacker.example') as client:
            return await client.get('/dashboard')

    assert asyncio.run(request_with_untrusted_host()).status_code == 403

    async def request_cross_origin_post():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            return await client.post(
                '/dashboard-api/ats/score',
                headers={'Origin': 'http://attacker.example'},
            )

    assert asyncio.run(request_cross_origin_post()).status_code == 403

    async def request_cross_origin_get():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            return await client.get(
                '/dashboard-api/ats/status/00000000-0000-0000-0000-000000000000',
                headers={'Origin': 'http://attacker.example'},
            )

    assert asyncio.run(request_cross_origin_get()).status_code == 403


def test_dashboard_proxy_uses_server_api_key(monkeypatch):
    import asyncio
    import httpx
    import api

    monkeypatch.setattr(api, 'API_KEY', 'dashboard-proxy-test-key')
    monkeypatch.setattr(api, 'DASHBOARD_ACCESS_KEY', '')

    async def request_proxy():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            return await client.get(
                '/dashboard-api/ats/status/00000000-0000-0000-0000-000000000000',
            )

    response = asyncio.run(request_proxy())

    assert response.status_code == 404
    assert response.json()['detail'] == 'Job not found'


def test_dashboard_proxy_forwards_resume_upload_without_browser_key(monkeypatch):
    import asyncio
    import httpx
    import api

    monkeypatch.setattr(api, 'API_KEY', 'dashboard-proxy-test-key')
    monkeypatch.setattr(api, 'DASHBOARD_ACCESS_KEY', '')
    monkeypatch.setattr(
        api,
        '_extract_with_timeout',
        lambda path, ext: {'raw_text': 'Candidate resume text', 'structural_issues': []},
    )
    monkeypatch.setattr(
        api,
        'parse_resume',
        lambda raw_text: {
            'name': 'Test Candidate',
            'contact': {},
            'rank': '',
            'skills': [],
            'sections': {},
            'raw_text': raw_text,
        },
    )
    received_options = {}

    def fake_score_layers(extracted, parsed, **options):
        received_options.update(options)
        return {
            'generic': {
                'total_score': 72,
                'grade': 'GOOD',
                'issues': [],
                'keyword_data': {},
            },
            'maritime': None,
            'overlay': False,
        }

    monkeypatch.setattr(api, 'score_layers', fake_score_layers)

    async def post_score_through_proxy():
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            return await client.post(
                '/dashboard-api/ats/score',
                headers={'Origin': 'http://localhost'},
                files={'file': ('resume.pdf', b'%PDF-1.4\nmock', 'application/pdf')},
                data={'career_track': 'generic', 'job_description': 'Deck officer'},
            )

    response = asyncio.run(post_score_through_proxy())
    assert response.status_code == 200, response.text
    assert response.json()['ats_score'] == 72
    assert received_options['career_track'] == 'generic'
    assert received_options['job_description'] == 'Deck officer'


def test_score_endpoint_includes_the_candidate_name(monkeypatch):
    """
    Real gap fixed after manual testing: the initial version of this
    endpoint returned scores/issues but not the candidate's name, even
    though it's already computed for free via build_candidate_profile()
    -- MYC's app would otherwise have no way to label a result without
    an extra round trip. Confirmed with a real document, not a mock.
    """
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    api.limiter.reset()
    client = TestClient(api.app)

    real_pdf = _real_pdf_fixture()
    with open(real_pdf, 'rb') as f:
        content = f.read()

    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('resume.pdf', content, 'application/pdf')},
    )
    assert r.status_code == 200
    assert r.json().get('name'), f'Expected a real, non-empty candidate name, got: {r.json().get("name")!r}'


def test_score_endpoint_works_for_a_real_docx_not_just_pdf(tmp_path, monkeypatch):
    """
    Confirmed end-to-end with a real .docx (not a mock or a PDF-only
    assumption) -- estimate_sea_service()'s pdf_path check correctly
    skips the PDF-table extraction path for a non-.pdf tmp_path and
    falls back to text-based sea-months parsing, same as re-scoring a
    generated DOCX already does elsewhere in the pipeline.
    """
    from fastapi.testclient import TestClient
    from docx import Document
    import api

    doc = Document()
    doc.add_paragraph('JANE TEST OFFICER')
    doc.add_paragraph('jane.test@example.com | +91 9876543210')
    doc.add_paragraph('SUMMARY')
    doc.add_paragraph('Third Officer with 5 years of sea service on oil tankers.')
    doc.add_paragraph('CERTIFICATIONS')
    doc.add_paragraph('STCW Basic Safety Training (BST)')
    doc.add_paragraph('Certificate of Competency (CoC)')
    doc.add_paragraph('GMDSS/GOC')
    doc.add_paragraph('ENG1 Medical Fitness Certificate')
    doc.add_paragraph('EXPERIENCE')
    doc.add_paragraph('Third Officer, AG Maritime Pvt Ltd, 01/2021 - 12/2023')
    doc.add_paragraph('Managed cargo operations and navigation watch.')
    docx_path = tmp_path / 'test.docx'
    doc.save(str(docx_path))

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    api.limiter.reset()
    client = TestClient(api.app)

    with open(docx_path, 'rb') as f:
        content = f.read()

    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('resume.docx', content,
                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['ats_score'] > 0
    assert body['rank_detected'] == 'third officer'
    assert body['cert_coverage'] == '3/3'


def test_score_endpoint_returns_422_for_a_genuinely_unreadable_file(monkeypatch):
    """Confirmed with a real corrupt PDF, not assumed -- the documented 422 behavior is real."""
    from fastapi.testclient import TestClient
    import api

    monkeypatch.setattr(api, 'API_KEY', 'score-endpoint-test-key')
    api.limiter.reset()
    client = TestClient(api.app)

    corrupt = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog'
    r = client.post(
        '/ats/score',
        headers={'X-API-Key': api.API_KEY},
        files={'file': ('corrupt.pdf', corrupt, 'application/pdf')},
    )
    assert r.status_code == 422
    assert 'PDF_READ_ERROR' in r.json()['detail']
