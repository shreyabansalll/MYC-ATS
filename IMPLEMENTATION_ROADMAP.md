# Intelligence Upgrade: Audit, Stage 1 Delivery, and Follow-up Plan

## Scope and safeguards

The existing worktree was inspected before changes. Its earlier uncommitted audit/hardening work was preserved. The production path remains `api.py` → extraction/parsing → `services/two_layer.py` → the existing generic ATS and optional maritime scorers → rewrite/generation for `/ats/submit`. Vacancy evidence is an additive overlay and does not modify tuned score weights.

No production service, external AI provider, candidate resume corpus, or credential was accessed. No deployment, commit, or push was performed. No model or library dependency was added for this stage.

## Existing feature versus gap matrix

| Area | Existing implementation | Stage 1 result / remaining gap |
|---|---|---|
| Vacancy-aware ATS | Explicit `job_description` already feeds deterministic generic keyword scoring; generic/rank templates are used when absent. | Added distinct vacancy requirement assessment only for an explicitly supplied vacancy. Existing ATS score is unchanged. Extraction is dictionary/catalog based; unrecognized free-form requirements need human review. |
| Maritime scoring | Deterministic maritime score, rank rules, certificate matrix and optional overlay. | Retained. Vacancy eligibility and score are separate and cannot override mandatory evidence review. |
| Skill gaps | Generic missing-keyword list existed but was not a structured evidence report. | Added demonstrated, possible, not-found and verification groupings with a source snippet and confidence. Partial lexical overlap is only a possible match. No semantic model or fuzzy certificate matching is used. |
| Certificate evidence | Existing `check_cert_evidence` distinguished evidenced, not evidenced, confirmed missing, expired and not applicable. | Added explicit issue/expiry date parsing and `VALID`, `EXPIRING_SOON`, `EXPIRED`, `EXPIRY_UNKNOWN`, `DATE_AMBIGUOUS`, and `REQUIRES_VERIFICATION` states. No expiry duration is inferred. |
| Score explanation | Generic score already returned parse/content points and content sub-scores. | Exposed a structured explanation with 40/60 composition and sub-scores through both score and result responses. Vacancy and maritime signals remain separate. |
| Evidence and confidence | Candidate profile had normalized values but not per-vacancy evidence. | Vacancy results carry evidence snippets, status and confidence. Contact-like values are redacted from snippets. Broader per-entity provenance for every extraction stage remains a gap. |
| Benchmarks | Synthetic harness exists; historical 269-resume batch is scores-only and not a current rewrite evaluation. | Added synthetic unit/regression cases. No labeled vacancy/candidate set is available, so no real-world accuracy or ranking lift is claimed. |
| OCR | No OCR fallback; scanned PDFs are flagged. | Added optional page-level local Tesseract fallback for image pages with little/no text, plus page cap, timeout, DPI, disable flag and explicit failure signals. Tesseract is not installed in this environment, so OCR recognition quality is not validated here. |
| Maritime NER / RapidFuzz | Existing spaCy, regex, aliases and deterministic rank/certificate logic. | No new NER model or fuzzy matching added. First produce labeled extraction cases; fuzzy certificate matches must remain verification-only. |
| Semantic matching / BM25 | Existing TF-IDF/cosine and whole-term matching must be preserved. | Deferred pending a held-out vacancy benchmark and CPU/latency measurements. |
| Recruiter feedback and preference learning | `training/prelabel.py` is an offline legacy Ollama pre-labeling utility, not RLHF. | No feedback store or training pipeline added. Requires explicit consent, access/retention rules and a reviewed dataset before collection/training. |
| Cover letters, LinkedIn, multilingual support, outcomes | Not in the live pipeline. | Deferred as separate product decisions; they must reuse verified candidate facts and remain isolated from ATS scoring. |

## Stage 1 architecture and API changes

New `services/vacancy_match.py` extracts supported rank, skill, certificate, vessel-type, and experience criteria from a caller-supplied vacancy, then reports evidence-backed matches and gaps. A missing certificate/skill in the resume yields review status, not an assertion that the candidate lacks it. Explicitly stated missing certificates can block eligibility; absent evidence cannot.

New `services/certificate_validity.py` reads only dates explicitly printed alongside recognized certificate names. Month-only expiry dates are treated as the end of that month; issue dates use the first day where only month/year is provided. The six-month warning period is configurable through `CERT_EXPIRING_SOON_DAYS` (default `183`). This evaluates document dates only, not the underlying qualification's legal validity.

`/ats/score` returns `vacancy_match`, `certificate_validity`, and `score_explanation`. `/ats/submit` persists these as JSON and `/ats/result/{job_id}` returns them. The existing additive SQLite column reconciliation adds the new nullable columns to existing databases at startup; it does not drop or rewrite existing data. Existing request fields and score fields remain unchanged.

Files added or changed for this Stage 1 delivery:

- Added `services/vacancy_match.py`, `services/certificate_validity.py`, and `tests/test_vacancy_match.py`.
- Updated `services/two_layer.py`, `api.py`, `config.py`, and `env_template.txt`.
- Updated `tests/test_domain_rules.py`, `CLAUDE.md`, and `INTEGRATION_GUIDE.md`.
- Corrected trailing whitespace in a pre-existing modified line in `pipeline/parser.py` so `git diff --check` passes.

No dependency was added. No scoring point values or thresholds were changed. The existing Claude rewrite provider remains in place.

## Validation and benchmark limits

- Pre-change test baseline run during this task: **225 passed, 9 warnings**.
- Final full suite recorded for Stage 1 plus optional OCR: **238 passed, 9 warnings** (final rerun in progress after the default OCR limit adjustment).
- Focused vacancy/domain/OCR tests: **23 passed, 6 warnings**.
- `py_compile` and `git diff --check`: passed.
- Synthetic evaluation: passed on generated fictional PDF/DOCX inputs (4/4 text markers and 5/5 selected fields per format; generic score 60, keyword match 100%, maritime score 22 for these fixtures). This is regression evidence only, not a representative benchmark.
- No latency/P95, memory, vacancy ranking, OCR accuracy, extraction precision/recall, recruiter preference, or cost comparison was measured. No labeled validation set exists in the repository.
- OCR applies only to image pages with fewer than 30 extracted text characters, defaults to at most two pages with a six-second Tesseract subprocess timeout per page, and has no hosted inference cost. The enclosing extraction worker has a 30-second timeout; benchmark worst-case PDF render/OCR latency before increasing limits.
- The test warnings are existing PyMuPDF/spaCy deprecations and FastAPI/Starlette lifecycle/TestClient deprecations.

## Remaining roadmap and acceptance gates

1. **Tesseract OCR validation:** install Tesseract on a staging host, run page-level fallback tests against authorized scanned/mixed documents, and measure OCR field accuracy and runtime. No installation or model download occurs automatically at deploy/startup.
2. **Entity provenance and normalization:** add source offsets/snippets and extraction-method confidence to rank/certificate/vessel/service-period entities; keep deterministic aliases first. Validate precision/recall on a consented labeled set before changing current extraction behavior.
3. **Semantic retrieval:** compare current TF-IDF/cosine and keyword behavior against optional BM25/sentence-transformer candidates on held-out vacancies. Add no model or cache until quality and CPU/P95 memory/latency justify it. Review model licenses before selecting any weights.
4. **Feedback and offline preference data:** design validated, consented recruiter-feedback records with separate storage, pseudonymous references, access and retention controls, duplicate/leakage checks, and reviewer authorization. Do not train from processed resumes automatically.
5. **SFT/DPO experiments:** after enough approved preference pairs exist, build a separate operator-invoked Hugging Face TRL/PEFT pipeline, split by source/candidate before training, evaluate held-out factuality and recruiter preference, and export artifacts outside production. DPO is preference optimization, not traditional RLHF. Keep Claude until a local model demonstrates comparable quality and commercial licensing is approved.
6. **Future product modules:** placement outcomes, cover letters, LinkedIn and multilingual support require MYC product requirements and separate evaluation; none should affect generic ATS weights by default.

Before deployment, run the database migration/startup checks against a staged copy, verify the API response against MYC's client integration, and benchmark with an authorized vacancy/resume set. No deployment migration was executed here.

## Post-verification follow-up implementation

The following optional work was added after the read-only audit and targeted defect fixes:

- `services/semantic_matcher.py` provides a separately measurable local CPU semantic signal. It loads lazily from an already-present model directory (`local_files_only=True`), never downloads at startup, caches only hashed requirement embeddings, excludes certificate requirements, and marks all matches for human verification. `requirements-ml.txt` keeps its libraries out of production requirements. No model was configured in this environment.
- `evaluation/run_matching_benchmark.py` compares existing ATS, vacancy keyword/match, TF-IDF, BM25, semantic, and exploratory equal-weight hybrid rankings with P@K/R@K/F1@K/NDCG@K. BM25 and hybrid are evaluation-only. The bundled examples are synthetic, excluded from training, and too small for quality claims.
- `pipeline/entity_evidence.py` combines existing deterministic extraction with a cached spaCy EntityRuler/PhraseMatcher layer and returns normalized entity, source offsets, redacted evidence, method, confidence and verification status. API score output and job result persistence include this additive field. `rules/maritime_ontology.py` derives a small vocabulary map from existing canonical registries rather than duplicating them.
- `training/feedback_schema.py` defines validated, consent-referenced and pseudonymous feedback records plus source-group split isolation. It does not store data or train models. Future TRL/PEFT/DPO work remains offline and contingent on approved, reviewed preference data.
- API result compatibility now defaults old rows to `{}` for object-valued additions; certificate evidence is redacted and expiry dates are associated only with dates following an expiry label.

The benchmark was run locally. All deterministic methods scored P@2/R@2/F1@2=1.0 on the tiny synthetic set; TF-IDF NDCG@2 was 0.8983, while other available deterministic signals were 1.0. Semantic metrics were unavailable. These values validate harness wiring only and do not support a production scoring change or accuracy claim.

## Substantive maritime and matching implementation — 2026-10-31

- Added `services/maritime_certificates.py`, `sea_service.py`, `vessel_intelligence.py`, and `rank_intelligence.py`; `services/maritime_intelligence.py` composes evidence-first certificate resolution, interval/overlap-aware service totals, vessel categories/propulsion, and separate MYC-policy eligibility guidance. Resume claims remain unverified and certificate similarity never establishes equivalence.
- `services/vacancy_match.py` now emits structured rank/certificate/experience/vessel/skill requirements and unresolved recruiter-review items. `services/candidate_matching.py` and `POST /ats/compare` provide multi-resume comparison with explicit factors while keeping eligibility separate from ordering and the existing ATS score.
- Rewrite integrity now records claim-source support and flags unsupported vessel-type additions. This is a heuristic review signal, not comprehensive fact verification.
- Added `training/prepare_feedback.py` for redacted, permission-restricted, source-group-isolated offline datasets, plus operator-invoked `train_dpo.py` (TRL/PEFT) and `train_lambdamart.py`. No data was collected and neither model was trained; these require approved real labelled feedback.
- `evaluation/run_maritime_intelligence_demo.py` is fictional-only. Full tests pass (261 passed, 9 warnings); no MYC-labelled benchmark was available. The `>=2 years` portal wording conflict remains unresolved as previously recorded.
