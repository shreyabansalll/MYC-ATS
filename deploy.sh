#!/bin/bash
# deploy.sh — run on your VPS to start the production API
# Tested on Ubuntu 22.04, Python 3.11

set -e

echo "=== Marine Your Career ATS — Production Deploy ==="

# 1. Python env
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -q --upgrade pip
python3 -m pip install -q -r requirements.txt

# Install spaCy model only if missing
python3 - <<'PY'
import spacy.util as u
if not u.is_package('en_core_web_lg'):
    import subprocess
    subprocess.check_call(['python3', '-m', 'spacy', 'download', 'en_core_web_lg', '-q'])
else:
    print('spaCy model en_core_web_lg already installed')
PY

# 2. Verify env file
if [ ! -f .env ]; then
    echo "ERROR: .env file missing — copy env_template.txt to .env and fill in values"
    exit 1
fi

if grep -q "generate-a-strong-random-key" .env; then
    echo "ERROR: MYC_API_KEY not set in .env"
    exit 1
fi

# 3. Create required directories
mkdir -p data/outputs /tmp/ycm-ats

# 4. Start API (2 workers — safe for BackgroundTasks + SQLite WAL)
echo "Starting API on port 8000..."
uvicorn api:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 2 \
    --log-level warning \
    --access-log \
    --no-server-header
