#!/usr/bin/env bash
# CedarStar: PostgreSQL + chroma_db + .env backup, upload to R2, prune old local archives.

set -uo pipefail
shopt -s extglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
DUMP_PATH="/tmp/cedarstar_db.dump"
BACKUP_ROOT="/home/backups/cedarstar"
RCLONE_REMOTE="cloudflare_r2:cedarstar-backup"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "开始备份流程"

# --- 1. 从 .env 读取 DATABASE_URL ---
log "步骤 1: 从 $ENV_FILE 读取 DATABASE_URL"
if [[ ! -f "$ENV_FILE" ]]; then
  log "失败: 未找到 $ENV_FILE"
  exit 1
fi

DATABASE_URL=""
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  line="${line##+([[:space:]])}"
  [[ -z "$line" ]] && continue
  if [[ "$line" == DATABASE_URL=* ]]; then
    val="${line#DATABASE_URL=}"
    if [[ "$val" == \"*\" ]]; then
      val="${val:1:-1}"
    elif [[ "$val" == \'*\' ]]; then
      val="${val:1:-1}"
    fi
    DATABASE_URL="$val"
    break
  fi
done < "$ENV_FILE"

if [[ -z "${DATABASE_URL:-}" ]]; then
  log "失败: .env 中未找到有效的 DATABASE_URL"
  exit 1
fi
log "步骤 1: 成功"

# --- 2. pg_dump ---
log "步骤 2: pg_dump 导出到 $DUMP_PATH"
if ! pg_dump -F c -f "$DUMP_PATH" "$DATABASE_URL"; then
  log "失败: pg_dump 执行失败"
  exit 1
fi
log "步骤 2: 成功"

# --- 3. tar 打包 ---
DATE_STR="$(date '+%Y%m%d')"
ARCHIVE_NAME="cedarstar_backup_${DATE_STR}.tar.gz"
ARCHIVE_PATH="${BACKUP_ROOT}/${ARCHIVE_NAME}"

log "步骤 3: 打包为 $ARCHIVE_PATH"
if ! mkdir -p "$BACKUP_ROOT"; then
  log "失败: 无法创建目录 $BACKUP_ROOT"
  exit 1
fi

if [[ ! -d "$SCRIPT_DIR/chroma_db" ]]; then
  log "失败: 目录不存在 $SCRIPT_DIR/chroma_db"
  exit 1
fi

if ! tar -czf "$ARCHIVE_PATH" -C /tmp "cedarstar_db.dump" -C "$SCRIPT_DIR" chroma_db .env; then
  log "失败: tar 打包失败"
  exit 1
fi
log "步骤 3: 成功"

# --- 4. rclone copy ---
log "步骤 4: rclone copy 推送到 $RCLONE_REMOTE"
if ! rclone copy "$ARCHIVE_PATH" "$RCLONE_REMOTE"; then
  log "失败: rclone copy 失败"
  exit 1
fi
log "步骤 4: 成功"

# --- 5. 删除临时 dump ---
log "步骤 5: 删除临时文件 $DUMP_PATH"
if ! rm -f "$DUMP_PATH"; then
  log "失败: 无法删除 $DUMP_PATH"
  exit 1
fi
log "步骤 5: 成功"

# --- 6. 清理超过 7 天的本地备份 ---
log "步骤 6: 删除 $BACKUP_ROOT 中超过 7 天的 .tar.gz"
if ! find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'cedarstar_backup_*.tar.gz' -mtime +7 -delete; then
  log "失败: find 清理失败"
  exit 1
fi
log "步骤 6: 成功"

log "备份流程全部完成"
exit 0

# -----------------------------------------------------------------------------
# crontab（每天东八区 UTC+8 凌晨 5:00，stdout/stderr 追加到日志）:
#
#   crontab -e
#
# 添加一行（请按实际项目根路径修改；TZ 保证按 Asia/Shanghai 的 5 点触发）:
#
#   0 5 * * * TZ=Asia/Shanghai /opt/cedarstar/backup.sh >> /var/log/cedarstar_backup.log 2>&1
#
# -----------------------------------------------------------------------------
