# Codebase Audit

## Scope and evidence

This audit covers the tracked application, pipeline, generic ATS layer, API, deployment configuration, tests, and integration/architecture documentation. It traces the production flow from upload through extraction, parsing, scoring, rewriting, document generation, re-scoring, persistence, download, and cleanup.

Local `.env` values and ignored resume/job data were not inspected or copied. No production endpoint, server, or credential was accessed. The security review was read-only; all changes below are local repository changes only. `pip-audit` was run against `requirements.txt`; no production network scan was run.

The service is a deterministic two-layer scorer with optional Claude rewriting; it is not a model trained on a proprietary resume corpus. The prior 269-resume single-layer before/after results do not measure the current generic scorer.

## Verified findings and disposition

| Priority | Finding | Evidence / impact | Disposition |
|---|---|---|---|
| Medium | The checked-in production launch paths previously bound Uvicorn to `0.0.0.0:8000` without configuring TLS. | `deploy.sh` and `marine-ats.service` were reachable on all interfaces if host firewall rules allowed it. Actual proxy/firewall state cannot be inferred from the repository. | Both production launch paths now bind `127.0.0.1`; a Caddy TLS site template and host/provider firewall checklist are included. Installing/enabling Caddy, setting the real DNS name, and applying/validating firewall rules remain operator steps; no live host was accessed. |
| High | Admin metrics previously accepted the client key when a distinct admin key was absent. | A client-facing credential could expose operational metrics. | Removed the fallback. Production startup requires distinct `MYC_API_KEY` and `MYC_ADMIN_KEY`; key comparisons use `hmac.compare_digest`. |
| High | API upload handlers read complete file contents before enforcing the configured byte cap; multipart parsing itself also accepted an unbounded request body. | A request could consume avoidable memory and temporary disk before application-level validation. | Added a bounded ASGI request-body guard for upload routes (`MAX_FILE_MB` plus 256 KiB multipart overhead) and a chunked upload reader that stops at one byte over the file limit. |
| Medium | Deletion and expiry cleanup trusted persisted output paths and recursively removed their parent directory. | A malformed or tampered database path could cause deletion outside the intended per-job output folder. | Downloads now resolve only expected per-job or known legacy output files. Deletion and cleanup validate paths and remove only the job folder or exact legacy filenames; unsafe rows are retained and logged. |
| Medium | Failed jobs without `expires_at` were excluded from TTL cleanup. | Failed-job metadata and artifacts could outlive the configured retention period. | Failed rows without an expiry are eligible once older than `JOB_TTL_HOURS`; pipeline failures set an expiry and attempt output cleanup. |
| Medium | Generated resume files used default filesystem permissions. | Candidate documents could be readable by other local users when directory permissions allowed it. | Upload DOCX files are created as `0600`; generated DOCX files are also `0600`, and job output directories are `0700`. |
| Medium | Rewrite execution did not use the existing numeric novelty helper to gate unsupported added metrics. | A generated experience bullet or summary could add a new numeric achievement and still reach scoring/output. | The API now rejects new unit-bearing numeric claims in the generated summary and experience bullets, preserves source-derived content, and records a distinct issue. This remains a heuristic, not comprehensive factual verification. |
| High | The dependency manifest resolved to eight packages with known advisories, including the multipart parser, web framework dependency, PDF parser, image library, and cryptography. | `pip-audit` reported 112 advisory records across eight packages; some records are duplicate aliases. | Updated the affected direct and transitive pins, including FastAPI/Starlette/Python Multipart, Pillow, python-dotenv, cryptography, Gunicorn, pytest, PDFPlumber, and pdfminer.six. The final `pip-audit` run reported no known vulnerable packages. |
| Low | `/health` returned `"ok"` even when its database query failed. | Load balancers and operators could receive a success-shaped response with `total_jobs: -1`. | Health now reports `database_ok: false` and `"status": "degraded"` for a database failure; low disk also degrades status. |
| Low | CI tests ran only on pushes to `main`. | Pull requests could merge without the checked-in test workflow running. | Added the `pull_request` trigger. |
| Low | Operational documentation described stale scorer validation, calibration, CORS, and deployment behavior. | Readers could incorrectly quote legacy before/after gains for the current scorer or deploy using public-interface binding. | Updated the architecture and integration guides with the measured scores-only batch, corrected calibration, production settings, and limits. |

## Residual risks and limitations

- The repository includes a Caddy TLS reverse-proxy template and firewall checklist, but does not install Caddy or change firewall settings. Production exposure and certificate issuance/renewal remain unverified until the operator configures the real DNS name, enables the proxy, and verifies host/provider firewall rules. Port 8000 must remain loopback-only.
- `slowapi` rate-limit state is process-local, so `--workers 2` can allow roughly twice its configured per-IP limit. The service-wide concurrent-job cap is database-backed; the per-IP rate limiter is not.
- `BackgroundTasks` is not a durable queue. A process crash can leave a job in `processing` and consume a concurrency slot until manually remediated.
- The numeric-claim gate is intentionally narrow. It checks unit-bearing numeric claims in summary/bullet text; it does not prove source claims true, detect nonnumeric fabrication, or cover every rendered field. Rank ordinals and other unitless numbers are excluded to avoid rejecting common title-format changes.
- Prompt-injection sanitization is pattern-based and is not a general LLM isolation boundary. Human review remains appropriate for generated resumes.
- File extension and magic-byte checks are not malware scanning or full format validation. Extraction has a wall-clock timeout, but parser/library vulnerabilities and malformed-file edge cases remain possible.
- CORS is not authentication. Production origins must be exact and must not include `*`; API keys must never be placed in browser code.
- SQLite, in-process background work, and per-worker metrics remain single-service tradeoffs documented in `CLAUDE.md`.
- Historical 269-candidate improvements (ATS 63.4→69.1 and maritime 54.1→70.7) came from the prior scorer. A prior local run of the then-current two-layer scorer was scores-only: 269 scored, 0 errors; generic average 57.0, median 58, range 24–75; grades 162 FAIL, 106 PASS, 1 GOOD; maritime scored 254 with average 76.0. The batch was not rerun after the PDF parser dependency update, and it is not a rewrite-improvement result.
- The declared requirements resolve cleanly and `pip-audit` reports no known vulnerabilities. `pip check` in the pre-existing local virtual environment flags undeclared extras (`svix`/Pydantic and `pytest-asyncio`/pytest constraints); a clean requirements-only resolver does not include them. Production deployment was not performed or validated.

## Validation performed

The initial pre-change test baseline was 197 passing tests. The post-change test results are recorded in `FIX_LOG.md`; these results validate repository tests only and do not substitute for production deployment checks or a full 269-resume rewrite batch.
