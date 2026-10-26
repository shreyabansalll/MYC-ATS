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
#
# Real gap fixed, confirmed by walking through this script against a
# genuinely bare VPS assumption: `sudo apt-get` unconditionally assumed
# `sudo` itself is installed. Ubuntu Cloud images ship it, but a bare
# Debian VPS (root login, no sudo package) does NOT -- "sudo: command
# not found" would kill this script on `set -e` before it ever got to
# installing anything. Root doesn't need sudo at all; a non-root user
# without sudo installed is a real dead end this script can't fix for
# them, but it can at least avoid failing on the CORRECT case (root).
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "ERROR: not running as root and 'sudo' isn't installed."
        echo "       Either run this script as root, or install sudo first"
        echo "       (as root: apt-get install -y sudo), then re-run."
        exit 1
    fi
fi

if command -v apt-get >/dev/null 2>&1; then
    if ! dpkg -s python3-venv >/dev/null 2>&1; then
        echo "Installing python3-venv (required for python3 -m venv)..."
        $SUDO apt-get update -qq && $SUDO apt-get install -y python3-venv
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

python3 - <<'PY'
import sys
from dotenv import dotenv_values

values = dotenv_values('.env')
api_key = values.get('MYC_API_KEY')
admin_key = values.get('MYC_ADMIN_KEY')
if not api_key or api_key == 'generate-a-strong-random-key-here':
    sys.exit('ERROR: set a real MYC_API_KEY in .env')
if not admin_key or admin_key == 'generate-a-different-strong-random-key-here':
    sys.exit('ERROR: set a real MYC_ADMIN_KEY in .env')
if api_key == admin_key:
    sys.exit('ERROR: MYC_ADMIN_KEY must differ from MYC_API_KEY')
PY

# ANTHROPIC_API_KEY left as env_template.txt's literal placeholder is a
# warning, not a hard exit: the service still starts and scoring-only
# requests work, but resume rewrites need a real Anthropic key.
if grep -q "your-anthropic-api-key-here" .env; then
    echo "WARNING: ANTHROPIC_API_KEY is still set to its placeholder value in .env"
    echo "         The service will start and /ats/score will work fine, but"
    echo "         AI rewrite/tailoring requests need a real Anthropic key."
    echo "         Set one before relying on the rewrite feature."
fi

# 3. Create required directories
# TMP_DIR defaults to /tmp/ycm-ats (matching api.py's own default), but
# an operator can override it in .env -- read the configured value here
# too, so this doesn't create the wrong directory if it was customized.
TMP_DIR_CONFIGURED=$(grep -E '^TMP_DIR=' .env | cut -d '=' -f2- || true)
mkdir -p data/outputs "${TMP_DIR_CONFIGURED:-/tmp/ycm-ats}"

# 4. Start API (2 workers — safe for BackgroundTasks + SQLite WAL)
#
# NOTE: this runs uvicorn directly in the foreground — it exits the
# moment this shell session ends, with no auto-restart on crash. For a
# real deploy, use the systemd unit at marine-ats.service instead (see
# its header comments for the paths/user to adjust first):
#   sudo cp marine-ats.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now marine-ats
# Run this script's steps 1-3 first either way — the unit file assumes
# venv/ and .env already exist at the deploy path.
echo "Starting API on port 8000..."
export APP_ENV=production
uvicorn api:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 2 \
    --log-level warning \
    --access-log \
    --no-server-header
