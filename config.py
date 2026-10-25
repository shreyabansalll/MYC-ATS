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

# ── Ollama (Local NER pre-labeling) ──────────────────────────────────
OLLAMA_BASE_URL    = 'http://localhost:11434/v1'
OLLAMA_MODEL       = 'qwen2.5:7b'

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