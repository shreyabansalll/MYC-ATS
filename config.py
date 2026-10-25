# config.py
# Central configuration — all API keys, model names, paths live here
# Never hardcode keys in pipeline files — always import from here

import os
from dotenv import load_dotenv

load_dotenv()  # Reads from .env file

# ── Groq API (AI Rewriting) ──────────────────────────────────────────
GROQ_API_KEY       = os.getenv('GROQ_API_KEY')
# llama-3.3-70b-versatile (itself a replacement for decommissioned
# llama3-70b-8192) is gone too -- confirmed via a real call, 404
# model_not_found, and absent from a real client.models.list() for this
# key. openai/gpt-oss-120b/20b (the closest replacement tier) don't fit
# this account's 8000 TPM on-demand limit for this app's real prompt
# sizes -- a real rewrite prompt is ~4000 input tokens, and these are
# reasoning models where hidden reasoning tokens compete with the JSON
# output for the same completion budget, confirmed truncating a real
# response even before hitting the TPM wall outright on a second call.
# qwen/qwen3.6-27b's tokenizer is dramatically more efficient for this
# content (424 tokens for the same ~4000-token prompt in a real test),
# comfortably fitting the current tier with no billing change -- traded
# off against needing real prompt/parsing changes for its different
# behavior (reasoning embedded as <think> tags inside the response
# content itself, rather than Groq's separate `reasoning` field; a
# different default JSON schema unless the prompt constrains it
# explicitly) -- see _strip_think_tags() and the schema instructions in
# build_rewrite_prompt().
GROQ_MODEL         = 'qwen/qwen3.6-27b'
GROQ_PRELABEL_MODEL = GROQ_MODEL

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