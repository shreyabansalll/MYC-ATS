# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Marine ATS — an Applicant Tracking System for maritime/seafarer resumes. Scores resumes 0-100 across format, sections, and keyword match. Includes AI rewriting via Groq API.

## Commands

```bash
# Run the ATS pipeline on a resume
venv/bin/python main.py <resume_file_path> [job_description]

# Example with custom job description
venv/bin/python main.py data/test_resumes/ShreyaBansal_Resume_v2.pdf "Seeking Python developer with FastAPI experience"
```

## Architecture

**Pipeline stages** (in `pipeline/`):
1. `extractor.py` — Text extraction from PDF (pdfplumber) or DOCX (python-docx). Detects ATS violations: tables, headers/footers, images, scanned PDFs.
2. `parser.py` — Regex + spaCy NER to extract contact info, name, sections, skills.
3. `scorer.py` — Scores 0-100: Format (40pts) + Sections (20pts) + Keywords (40pts via TF-IDF cosine similarity).
4. `ai_rewriter.py` — Groq API (llama-3.3-70b) rewrites resumes with maritime-specific prompts. Includes built-in JD templates for deck officer, chief officer, engineer, able seaman, rating.

**Key files:**
- `main.py` — Entry point, orchestrates pipeline
- `config.py` — API keys (GROQ_API_KEY from .env), model names, scoring weights, thresholds

**Scoring thresholds** (config.py):
- 85+ = EXCELLENT, 75+ = GOOD, 60+ = PASS, else FAIL

## Dependencies

No requirements.txt — environment managed via venv. Key packages: groq, spacy (en_core_web_lg), pdfplumber, python-docx, scikit-learn, python-dotenv.

## Data

- `data/test_resumes/` — Sample PDFs for testing
- `data/outputs/` — Output directory for processed results
- `data/maritime_skills.json` — Maritime skills taxonomy (referenced in config)
