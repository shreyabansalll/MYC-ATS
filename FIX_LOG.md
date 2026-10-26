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
