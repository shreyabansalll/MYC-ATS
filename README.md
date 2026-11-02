# Marine Your Career ATS

Marine ATS is a FastAPI service for scoring and tailoring maritime resumes. It combines deterministic generic ATS scoring, an optional maritime overlay, evidence-based vacancy matching, source-grounded Claude rewriting, and ATS-focused document generation.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ats/submit` | Submit an asynchronous scoring and rewrite job |
| `POST` | `/ats/score` | Run synchronous scoring without an LLM rewrite |
| `POST` | `/ats/compare` | Compare 2-10 resumes synchronously |
| `GET` | `/ats/status/{job_id}` | Poll an asynchronous job |
| `GET` | `/ats/result/{job_id}` | Retrieve a completed result |
| `GET` | `/ats/download/{job_id}` | Download the generated DOCX |
| `GET` | `/health` | Check service health |

See [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) for request and response examples.

## Provider configuration

Production resume rewriting uses Anthropic Claude. Provider and model settings are in [config.py](config.py); credentials belong in the environment or `.env`, which is not committed. See [env_template.txt](env_template.txt).

`training/prelabel.py` is a legacy, manually invoked Ollama utility only. It is not part of the live rewriting pipeline or deployment.

## Local setup

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env_template.txt .env
venv/bin/python -m pytest tests/
venv/bin/python -m evaluation.run_synthetic_evaluation
```

## Scoring and evidence

- Generic ATS and maritime scores are separate.
- Company/rank keyword matches are advisory signals, not score substitutions.
- ATS platform mappings are MYC-supplied operational assumptions, not independently verified employer configurations.
- A missing certificate in resume text is not proof that the candidate does not hold it.
- Vacancy matching requires an explicit vacancy job description.
- Synthetic evaluation uses fictional fixtures and is not a real-world accuracy benchmark.

See [CODEBASE_AUDIT.md](CODEBASE_AUDIT.md), [FIX_LOG.md](FIX_LOG.md), and [MYC_ATS_Technical_Architecture.docx](MYC_ATS_Technical_Architecture.docx).
