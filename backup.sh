#!/usr/bin/env bash
# CedarStar/CedarClio backup: PostgreSQL + ChromaDB + .env, upload to R2, prune local archives.

set -uo pipefail
shopt -s extglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

read_env_value() {
  local key="$1"
  local line val first last

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    line="${line##+([[:space:]])}"
    [[ -z "$line" ]] && continue

    if [[ "$line" == "$key="* ]]; then
      val="${line#*=}"
      if [[ ${#val} -ge 2 ]]; then
        first="${val:0:1}"
        last="${val: -1}"
        if { [[ "$first" == '"' ]] && [[ "$last" == '"' ]]; } || { [[ "$first" == "'" ]] && [[ "$last" == "'" ]]; }; then
          val="${val:1:${#val}-2}"
        fi
      fi
      printf '%s' "$val"
      return 0
    fi
  done < "$ENV_FILE"

  return 1
}

log "Starting backup"

# --- 1. Read backup config from .env ---
log "Step 1: reading backup config from $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
  log "Failed: missing $ENV_FILE"
  exit 1
fi

APP_NAME="$(read_env_value APP_NAME || true)"
APP_NAME="${APP_NAME:-cedarstar}"
DATABASE_URL="$(read_env_value DATABASE_URL || true)"
CHROMADB_PERSIST_DIR="$(read_env_value CHROMADB_PERSIST_DIR || true)"
CHROMADB_PERSIST_DIR="${CHROMADB_PERSIST_DIR:-chroma_db}"

BACKUP_DUMP_PATH="$(read_env_value BACKUP_DUMP_PATH || true)"
BACKUP_ROOT="$(read_env_value BACKUP_ROOT || true)"
BACKUP_RCLONE_REMOTE="$(read_env_value BACKUP_RCLONE_REMOTE || true)"
BACKUP_ARCHIVE_PREFIX="$(read_env_value BACKUP_ARCHIVE_PREFIX || true)"
BACKUP_RETENTION_DAYS="$(read_env_value BACKUP_RETENTION_DAYS || true)"

DUMP_PATH="${BACKUP_DUMP_PATH:-/tmp/${APP_NAME}_db.dump}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/backups/${APP_NAME}}"
RCLONE_REMOTE="${BACKUP_RCLONE_REMOTE:-cloudflare_r2:${APP_NAME}-backup}"
ARCHIVE_PREFIX="${BACKUP_ARCHIVE_PREFIX:-${APP_NAME}_backup}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  log "Failed: DATABASE_URL is missing in .env"
  exit 1
fi
if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  log "Failed: BACKUP_RETENTION_DAYS must be a non-negative integer, got $RETENTION_DAYS"
  exit 1
fi
log "Step 1: ok (APP_NAME=$APP_NAME)"

# --- 2. pg_dump ---
log "Step 2: dumping PostgreSQL to $DUMP_PATH"
if ! pg_dump -F c -f "$DUMP_PATH" "$DATABASE_URL"; then
  log "Failed: pg_dump failed"
  exit 1
fi
log "Step 2: ok"

# --- 3. tar archive ---
DATE_STR="$(date '+%Y%m%d')"
ARCHIVE_NAME="${ARCHIVE_PREFIX}_${DATE_STR}.tar.gz"
ARCHIVE_PATH="${BACKUP_ROOT}/${ARCHIVE_NAME}"
DUMP_DIR="$(dirname "$DUMP_PATH")"
DUMP_FILE="$(basename "$DUMP_PATH")"

log "Step 3: creating archive $ARCHIVE_PATH"
if ! mkdir -p "$BACKUP_ROOT"; then
  log "Failed: cannot create $BACKUP_ROOT"
  exit 1
fi

if [[ ! -d "$SCRIPT_DIR/$CHROMADB_PERSIST_DIR" ]]; then
  log "Failed: missing $SCRIPT_DIR/$CHROMADB_PERSIST_DIR"
  exit 1
fi

if ! tar -czf "$ARCHIVE_PATH" -C "$DUMP_DIR" "$DUMP_FILE" -C "$SCRIPT_DIR" "$CHROMADB_PERSIST_DIR" .env; then
  log "Failed: tar failed"
  exit 1
fi
log "Step 3: ok"

# --- 4. rclone copy ---
log "Step 4: copying archive to $RCLONE_REMOTE"
if ! rclone copy "$ARCHIVE_PATH" "$RCLONE_REMOTE"; then
  log "Failed: rclone copy failed"
  exit 1
fi
log "Step 4: ok"

# --- 5. Remove temporary dump ---
log "Step 5: removing temporary dump $DUMP_PATH"
if ! rm -f "$DUMP_PATH"; then
  log "Failed: cannot remove $DUMP_PATH"
  exit 1
fi
log "Step 5: ok"

# --- 6. Prune old local archives ---
log "Step 6: pruning .tar.gz files older than $RETENTION_DAYS days in $BACKUP_ROOT"
if ! find "$BACKUP_ROOT" -maxdepth 1 -type f -name "${ARCHIVE_PREFIX}_*.tar.gz" -mtime "+$RETENTION_DAYS" -delete; then
  log "Failed: find prune failed"
  exit 1
fi
log "Step 6: ok"

log "Backup complete"
exit 0

# -----------------------------------------------------------------------------
# Optional .env overrides:
#
#   APP_NAME=cedarstar
#   CHROMADB_PERSIST_DIR=chroma_db
#   BACKUP_DUMP_PATH=/tmp/cedarstar_db.dump
#   BACKUP_ROOT=/home/backups/cedarstar
#   BACKUP_RCLONE_REMOTE=cloudflare_r2:cedarstar-backup
#   BACKUP_ARCHIVE_PREFIX=cedarstar_backup
#   BACKUP_RETENTION_DAYS=7
#
# Crontab example, 05:00 Asia/Shanghai every day:
#
#   0 5 * * * TZ=Asia/Shanghai /opt/cedarstar/backup.sh >> /var/log/cedarstar_backup.log 2>&1
#
# -----------------------------------------------------------------------------
