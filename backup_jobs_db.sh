#!/bin/bash
# backup_jobs_db.sh — daily backup of data/jobs.db to a second location.
#
# Uses sqlite3's ".backup" command, not a plain `cp` — per CLAUDE.md's
# "Backup / restore" section, data/jobs.db runs in WAL mode, and a raw
# file copy while the API is live can miss uncommitted state sitting in
# jobs.db-wal/jobs.db-shm. `.backup` handles this correctly even with
# concurrent readers/writers, so this is safe to run on a live server.
#
# Usage: ./backup_jobs_db.sh [backup_dir]
#   backup_dir defaults to /var/backups/marine-ats (override via arg,
#   or the BACKUP_DIR env var — the positional arg wins if both given).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DB="$REPO_DIR/data/jobs.db"
BACKUP_DIR="${1:-${BACKUP_DIR:-/var/backups/marine-ats}}"
RETENTION_DAYS=14

if [ ! -f "$SOURCE_DB" ]; then
    echo "ERROR: $SOURCE_DB not found — nothing to back up" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEST_FILE="$BACKUP_DIR/jobs_${TIMESTAMP}.db"

sqlite3 "$SOURCE_DB" ".backup '$DEST_FILE'"

if [ ! -s "$DEST_FILE" ]; then
    echo "ERROR: backup at $DEST_FILE is missing or empty" >&2
    exit 1
fi

echo "Backed up $SOURCE_DB -> $DEST_FILE ($(du -h "$DEST_FILE" | cut -f1))"

# Prune backups older than RETENTION_DAYS -- this is operational-continuity
# backup for data/jobs.db (job metadata: rank/sea-months/cert-coverage,
# no raw resume text/PII per CLAUDE.md), not a long-term PII archive, so
# unbounded retention isn't the goal here.
find "$BACKUP_DIR" -maxdepth 1 -name 'jobs_*.db' -mtime "+${RETENTION_DAYS}" -print -delete
