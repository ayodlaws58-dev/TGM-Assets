#!/usr/bin/env bash
# Nightly backup: MySQL dump + uploads and other local files. Keeps 14 days.
# Installed as /usr/local/bin/tgm-assets-backup and run by tgm-backup.timer.
set -euo pipefail

APP_DIR=/opt/tgm-assets
DEST=/var/backups/tgm-assets
KEEP_DAYS=14
STAMP=$(date +%Y-%m-%d_%H%M)

env_value() { grep -E "^$1=" "$APP_DIR/.env" | head -n1 | cut -d= -f2-; }

mkdir -p "$DEST"
umask 077

MYSQL_PWD=$(env_value MYSQL_PASSWORD) mysqldump --single-transaction --no-tablespaces \
  -h 127.0.0.1 -u "$(env_value MYSQL_USER)" "$(env_value MYSQL_DATABASE)" \
  | gzip > "$DEST/db_$STAMP.sql.gz"

tar --ignore-failed-read -czf "$DEST/files_$STAMP.tar.gz" -C "$APP_DIR" \
  data/asset_uploads data/logo.jpg .env 2>/dev/null || true

find "$DEST" -type f -mtime +"$KEEP_DAYS" -delete
echo "Backup written to $DEST ($STAMP)"
