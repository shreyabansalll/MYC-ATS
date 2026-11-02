# config.py
# Central configuration — all API keys, model names, paths live here
# Never hardcode keys in pipeline files — always import from here

import os
from dotenv import load_dotenv

load_dotenv()  # Reads from .env file

# ── Anthropic Claude (AI Rewriting) ──────────────────────────────────
# Resume rewriting and tailoring use Anthropic Claude.
# The key is read from the environment; keep it out of source control.
ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY')
CLAUDE_MODEL       = 'claude-haiku-5-5'

# ── File Paths ───────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.path.join(BASE_DIR, 'data')
TEST_RESUMES    = os.path.join(DATA_DIR, 'test_resumes')
OUTPUT_DIR      = os.path.join(DATA_DIR, 'outputs')
MARITIME_SKILLS = os.path.join(DATA_DIR, 'maritime_skills.json')

# ── Scoring Weights (from ATS research PDF) ──────────────────────────
FORMAT_WEIGHT   = 40
SECTION_WEIGHT  = 20
KEYWORD_WEIGHT  = 40

# ── ATS Thresholds ───────────────────────────────────────────────────
SCORE_EXCELLENT = 85
SCORE_GOOD      = 75
SCORE_PASS      = 60

# After rewrite, if generic ATS improvement is under this many points,
# retry once with a stricter prompt and keep the better of the two.
REWRITE_IMPROVEMENT_RETRY_POINTS = 15

# Tune this after running before/after evaluation on labelled examples.
COMPANY_KEYWORD_WEIGHT_MULTIPLIER: float = 1.5

# Certificate dates are parsed only when explicitly printed on the resume;
# this window changes status labels but never infers certificate validity.
CERT_EXPIRING_SOON_DAYS: int = int(os.getenv('CERT_EXPIRING_SOON_DAYS', '183'))
if CERT_EXPIRING_SOON_DAYS < 0:
    raise RuntimeError('CERT_EXPIRING_SOON_DAYS must be non-negative')

# Optional semantic signal. The model path must already exist locally; model
# loading uses local_files_only=True and is deferred until the first scored JD.
SEMANTIC_MATCH_ENABLED = os.getenv('SEMANTIC_MATCH_ENABLED', '0').casefold() in {'1', 'true', 'yes'}
SEMANTIC_MODEL_PATH = os.getenv('SEMANTIC_MODEL_PATH', '').strip()
SEMANTIC_MATCH_THRESHOLD = float(os.getenv('SEMANTIC_MATCH_THRESHOLD', '0.65'))
if not 0.0 <= SEMANTIC_MATCH_THRESHOLD <= 1.0:
    raise RuntimeError('SEMANTIC_MATCH_THRESHOLD must be between 0 and 1')
