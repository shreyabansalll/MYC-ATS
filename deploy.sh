#!/bin/bash
# deploy.sh — run on your VPS to start the production API
# Tested on Ubuntu 22.04, Python 3.11
#
# NOTE: Ubuntu 22.04 LTS's own apt-provided `python3` is 3.10, not 3.11 --
# this script does not install or select a 3.11 interpreter, it uses
# whatever `python3` already resolves to on the box. The dependency set
# is expected to work fine under 3.10 too, but if a real Python 3.11 is
# required, install it separately (e.g. via deadsnakes) and change the
# `python3` calls below to that interpreter before running this script.

set -e

echo "=== Marine Your Career ATS — Production Deploy ==="

# 1. Python env
# A fresh Ubuntu/Debian server image often lacks the python3-venv apt
# package, which `python3 -m venv` needs to bootstrap pip into the new
# environment -- without it this fails with "ensurepip is not available"
# on an otherwise-correct box. Installed proactively rather than only on
# failure, since apt install is a no-op (fast) if already present.
if command -v apt-get >/dev/null 2>&1; then
    if ! dpkg -s python3-venv >/dev/null 2>&1; then
        echo "Installing python3-venv (required for python3 -m venv)..."
        sudo apt-get update -qq && sudo apt-get install -y python3-venv
    fi
fi

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

# MYC_ADMIN_KEY is optional (falls back to MYC_API_KEY if genuinely
# unset — see api.py's require_admin_key) but if the line was copied
# from env_template.txt and left unedited, its placeholder text is
# public (checked into this repo) — accepting it as a "configured" key
# would be worse than the fallback, since it's a known value rather than
# an unset one.
if grep -q "generate-a-different-strong-random-key" .env; then
    echo "ERROR: MYC_ADMIN_KEY is still set to its placeholder value in .env"
    echo "       Either set it to a real, distinct key, or remove the line entirely"
    echo "       (falls back to MYC_API_KEY when unset — see env_template.txt)."
    exit 1
fi

# 3. Create required directories
# TMP_DIR defaults to /tmp/ycm-ats (matching api.py's own default), but
# an operator can override it in .env -- read the configured value here
# too, so this doesn't create the wrong directory if it was customized.
TMP_DIR_CONFIGURED=$(grep -E '^TMP_DIR=' .env | cut -d '=' -f2- || true)
mkdir -p data/outputs "${TMP_DIR_CONFIGURED:-/tmp/ycm-ats}"

# 4. Start API (2 workers — safe for BackgroundTasks + SQLite WAL)
echo "Starting API on port 8000..."
uvicorn api:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 2 \
    --log-level warning \
    --access-log \
    --no-server-header
