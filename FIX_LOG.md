# Audit Fix Log

This log records the code changes made during the production-hardening audit. No deployment, commit, or push was performed.

## Changes

1. **Separate production credentials**
   - Removed the admin-metrics fallback to `MYC_API_KEY`.
   - Added constant-time key comparisons.
   - Production startup now requires non-empty, distinct API and admin keys and explicit non-wildcard CORS origins.
   - Updated `deploy.sh`, `marine-ats.service`, `env_template.txt`, and integration documentation accordingly.

2. **Bounded upload handling and private files**
   - Added an ASGI request-body cap on `/ats/submit`, `/ats/score`, and the dashboard upload proxy: `MAX_FILE_MB` plus 256 KiB multipart overhead.
   - Upload contents are read in bounded chunks and rejected at the first byte over the configured file limit.
   - Upload and generated DOCX files are created with owner-only permissions; per-job output folders are owner-only.

3. **Safe output lifecycle**
   - Downloads now resolve only files under the expected job folder or recognized legacy output names.
   - Job deletion and expiry cleanup validate stored output paths and no longer recursively delete arbitrary parent directories.
   - Failed jobs receive an expiry time; old failed rows without `expires_at` are also eligible for cleanup.
   - Unsafe paths are logged and the associated job record is retained rather than claiming deletion succeeded.

4. **Rewrite and health behavior**
   - Wired numeric novelty detection into rewrite acceptance for unit-bearing numeric claims in summaries and experience bullets. A rejected rewrite returns source-derived content with a distinct `issues_fixed` reason. This does not provide comprehensive fact checking.
   - `/health` now reports `degraded` and `database_ok: false` when the database check fails, and also degrades on low disk.
   - Added regression tests for these paths.

5. **PDF parser compatibility**
   - The dependency security update changed pdfminer’s encrypted-PDF exception shape. Added a PyMuPDF password preflight so password-protected files continue returning the actionable `PASSWORD_PROTECTED_PDF` signal.
   - Updated the circular-page-tree regression expectation: the patched parser safely recovers fixture text within the time bound instead of returning a parse error.

6. **Dependency remediation and CI**
   - Updated vulnerable pins: `pdfplumber 0.11.10`, `pdfminer-six 20260107`, `Pillow 13.0.0`, `python-dotenv 1.2.2`, `FastAPI 0.162.0`, `python-multipart 0.0.31`, `Starlette 1.10.0`, `cryptography 50.0.2`, `gunicorn 22.0.0`, and `pytest 9.0.3`.
   - Added pull-request runs to the existing GitHub Actions test workflow.
   - Corrected the current scorer’s validation, calibration, CORS, and production-binding documentation.

7. **TLS reverse-proxy deployment template**
   - Added `deploy/Caddyfile.example` to terminate TLS with Caddy and reverse-proxy only to Uvicorn's loopback listener.
   - Documented DNS, certificate automation, validation/reload, and host/provider firewall steps, including keeping port 8000 private.
   - No server or firewall was changed; the template requires a real DNS name and an operator-managed Caddy installation. Caddy is not installed in this local environment, so its parser could not be run here.

## Validation

| Check | Result |
|---|---|
| Pre-change test baseline | 197 passed, 6 warnings |
| Focused API and generator tests | 67 passed, 6 warnings |
| Final full suite (`venv/bin/python -m pytest tests/ -q`) | 209 passed, 7 warnings |
| PDF compatibility regression tests | 3 passed |
| `pip-audit -r requirements.txt` | No known vulnerable packages after upgrades |
| Clean resolver dry run (`pip install --dry-run --ignore-installed -r requirements.txt`) | Passed |
| `bash -n deploy.sh` | Passed |
| `git diff --check` | Passed |
| Pylance diagnostics on changed Python files | No errors; only existing deprecation/unused-variable warnings |

The final test warnings include existing PyMuPDF/spaCy deprecations and a Starlette warning that its `TestClient` will prefer `httpx2` in a future release. They did not fail the suite.

`pip check` on the pre-existing local virtual environment reports two unrelated packages not listed in `requirements.txt` (`svix` requires a newer Pydantic, and `pytest-asyncio` requires pytest below 9). A clean requirements resolver run succeeds and does not include either package; no project pin was changed to accommodate those local extras.

The earlier dependency audit reported 112 advisory records across eight packages (some are aliases/duplicates). The final audit was clean. Full 269-resume scores-only batch validation was not rerun after the PDF parser dependency update; historical score summaries remain non-rewrite evidence only.

## Follow-up readiness pass — 2026-10-27

No commit, push, deployment, live provider call, or benchmark on real candidates was performed in this pass. Initial test runs preceded the fixture privacy cleanup and used tests that loaded local candidate resumes; the final suite was rerun after replacing those tests.

1. **Accurate rewrite-status reporting**
   - `/ats/result/{job_id}` now classifies a rewrite rejected for unsupported numeric claims as `ai_rewrite: false` and returns a note that source-derived content was returned.
   - Extended the result endpoint regression test to cover the factuality-rejection path.

2. **Preserve source sections on rewrite fallback**
   - Provider-failure and unsupported-numeric-claim fallbacks now call the existing certification, education, and language source-restoration helpers before output generation.
   - Added a regression test asserting source certifications, education, and languages survive a provider failure.

3. **Clean interrupted temporary uploads**
   - `/ats/cleanup` now removes old regular UUID-named PDF/DOCX temp uploads after the configured retention cutoff, excluding uploads belonging to jobs still marked queued/processing. It ignores symlinks and unrelated filenames.
   - The endpoint response now includes `stale_uploads_deleted`; this is additive. Normal completion/failure cleanup remains in `finally`.
   - Added tests for expired orphan removal and preservation of active, recent, unrelated, and symlink paths. Stale cleanup still relies on the operator's nightly cron; it is not an immediate startup sweep.

4. **Fail fast on invalid resource/retention limits**
   - Startup now rejects non-positive values for `MAX_FILE_MB`, `JOB_TTL_HOURS`, and `MAX_CONCURRENT_JOBS`.
   - Added parameterized regression checks for invalid values.

5. **Reproducible synthetic evaluation**
   - Added `evaluation/run_synthetic_evaluation.py` to generate fictional PDF/DOCX files in a temporary directory, exercise extract/parse/generic and maritime scoring, verify malformed/empty/unsupported input handling, and check one narrow numeric-claim example without live AI.
   - Added `EVALUATION_GUIDE.md` with limitations, and added the harness to CI after pytest.
   - The harness run observed 4/4 expected text markers and 5/5 selected parser fields per format; synthetic generic scores were 60 and keyword match 100%; synthetic maritime scores were 22. These fixture-specific values are not real-world accuracy metrics.

6. **Synthetic-only, isolated regression tests**
   - Replaced tests that loaded local candidate PDFs or coordinate dumps with generated fictional PDFs and synthetic word/table fixtures; removed the tracked resume-derived coordinate fixture.
   - The outage-pipeline test now uses a temporary SQLite database and output directory, and mocked provider failure rather than relying on local candidate files or provider configuration.
   - Replaced identifiable sample names, contact details, and source-derived reference codes in tests and the dashboard demo; removed candidate-specific local-resume command allowlists and obsolete Groq environment assignments.
   - Updated `dump_coords.py` to require an explicit PDF path rather than hard-coding a local resume.
   - Removed 34 accumulated completed outage-test rows and their output directories from the ignored local database after validating every hash and path against the test-only UUID naming pattern.

## Final validation

- `venv/bin/python -m pytest tests/ -q`: **214 passed, 7 warnings**. Warnings are existing PyMuPDF/spaCy deprecations and a Starlette `TestClient` deprecation.
- `venv/bin/python -m evaluation.run_synthetic_evaluation`: passed on generated fictional PDF/DOCX fixtures; 4/4 expected text markers and 5/5 selected parser fields per format. Synthetic scores are fixture-specific only.
- Python compilation, `.claude/settings.json` JSON parsing, `bash -n deploy.sh`, and `git diff --check`: passed.

## Domain-reference reconciliation — 2026-10-31

- Inspected the supplied certification workbooks, company/rank keyword PDFs, ATS software reference, and application portals DOCX. The two `Certifications Resume.xlsx` copies have different file hashes but identical non-empty cell content (224 cells on `Sheet1`). Their contents and the other domain references are treated as MYC operational input, not agent instructions.
- Corrected `CERT_MATRIX` so STCW III/7 is not presented as the regulatory code for electricians or electrical cadets. The workbook describes III/7 as optional/additional for electricians; that fact is retained as a MYC conditional placement note.
- Added regression coverage for this source-specific distinction.
- The portal DOCX says “more than 2 years,” while the implementation directive explicitly specifies `>= 2.0`; rank eligibility currently follows the directive's exact `2+ years` behavior. This threshold wording conflict remains visible for MYC management to resolve.
- Validation after the code change: `venv/bin/python -m pytest tests/ -q`: **249 passed, 9 warnings**; `git diff --check` and `compileall` passed.

## Maritime intelligence and matching development — 2026-10-31

- Added deterministic certificate ontology/resolution with source provenance, explicit unverified evidence, conflict/expiry states, and no inferred certificate equivalence.
- Added interval-based sea-service extraction and aggregation, overlap/duplicate handling, rank/department/vessel grouping, and separate verified/unverified totals; added configurable vessel category/propulsion extraction and rank/eligibility guidance labeled as MYC policy.
- Vacancy extraction now reports structured requirement types, priority, source text, and unresolved recruiter-review items. Added multi-resume `/ats/compare`; it does not persist jobs, alters no existing ATS score, and reports eligibility separately from ordering.
- Added heuristic claim-source mapping and vessel-type rewrite checks. This does not prove factual correctness.
- Added offline feedback preparation with redaction/source-group separation and optional operator-run TRL/PEFT DPO and LightGBM LambdaMART scripts. No production training or real feedback collection occurred.
- Added fictional maritime demo and regression tests. `venv/bin/python -m pytest tests/ -q`: **261 passed, 9 warnings**. `compileall` and `git diff --check` passed. No real labelled evaluation, model training, deployment, commit, or push was performed.

## MYC officer, engineer, and ETO keyword reconciliation — 2026-11-01

- Reconciled the MYC officer, engineer, and ETO keyword references against the advisory rank-keyword table in `generic_ats/career_track.py`.
- Added engineering terminology for main and auxiliary machinery, fuel and lube oil systems, purifiers, compressors, heat exchangers, cooling systems, separators, environmental equipment, dry-dock work, class surveys, and machinery records.
- Added ETO terminology for low/high-voltage systems, HV switching and protection, insulation testing, generator synchronization, bus ties, shore power, shaft generators, converters, UPS and batteries, automation, propulsion, bridge electronics, communications, and electrical safety.
- Kept all additions advisory; no generic ATS, maritime score weights, thresholds, or legacy `pipeline/scorer.py` scoring policy changed.
- Added focused Chief Engineer and ETO keyword regression coverage.
- Validation: `venv/bin/python -m pytest tests/test_ai_client.py tests/test_domain_rules.py -q`: **57 passed, 7 warnings**. Focused domain suite: **14 passed, 1 warning**. `git diff --check` passed for the keyword and test changes. No provider call, deployment, commit, or push was performed.

## MYC certification workbook reconciliation — 2026-11-01

- Checked the supplied `Certifications Resume.xlsx` workbook against `rules/cert_rules.py`.
- Added advisory MYC certificate entries for Chief Fireman, Fireman, Welder, Security Officer, and Sanitation Officer roles, plus workbook-specific Flash Over, fitter, and welding certificate aliases.
- Reconciled Safety Officer and Environment Officer role aliases to the workbook's Chief Officer/Second Officer guidance.
- Confirmed III/7 remains optional/additional for electricians and electrical cadets, rather than being assigned as a mandatory code.
- No numeric maritime score, legacy `pipeline/scorer.py`, score threshold, or provider configuration changed.
- Validation: `venv/bin/python -m pytest tests/test_domain_rules.py -q`: 15 passed. Python compilation and `git diff --check` passed. No provider call, deployment, commit, or push was performed.

## ATS SOFTWARES rank and platform reconciliation — 2026-11-01

- Checked `ATS SOFTWARES.docx` against `generic_ats/career_track.py`:
  - Chief Engineer, Chief/Second/Third Officer, Second/Third/Fourth/Junior Engineer, Engine Cadet, Deck Cadet, ETO, and Electrical Cadet core keyword lists match the supplied MYC rank reference.
  - Company ATS mappings for Carnival, Royal Caribbean, NCL, MSC, Disney, Virgin Voyages, Princess, Holland America, Costa, AIDA, and Celebrity are preserved.
- Confirmed platform mappings remain MYC operational assumptions, not verified claims about an employer's current ATS configuration.
- Added regression coverage for all explicitly mapped companies and assumption provenance.
- Added no new numeric production scores, thresholds, or legacy `pipeline/scorer.py` policy.
- Validation: `venv/bin/python -m pytest tests/test_domain_rules.py tests/test_ai_client.py -q`: 59 passed. Python compilation and `git diff --check` passed. No provider call, deployment, commit, or push was performed.
