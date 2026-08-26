#!/bin/bash
# Nightly backup for Bindex: one self-contained tar.gz per day containing
# a consistent SQLite snapshot (via the sqlite3 .backup hot-backup API, so
# it's safe to run while gunicorn is live) plus the uploaded photos/docs.
# Deliberately excludes .env (SECRET_KEY) — losing it on a restore just
# invalidates existing login sessions, it doesn't lose data, and it's not
# worth putting a secret in cloud storage.
set -euo pipefail

APP_DIR="$HOME/bindex"
BACKUP_DIR="$HOME/bindex-backups"
KEEP_DAYS=14
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$BACKUP_DIR"

# Uses Python's stdlib sqlite3.Connection.backup() (same underlying SQLite
# Online Backup API as the `sqlite3 .backup` CLI command) since the sqlite3
# CLI itself isn't installed on this box.
python3 - "$APP_DIR/instance/stock.db" "$WORK/stock.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close()
dst.close()
PY
cp -r "$APP_DIR/instance/uploads" "$WORK/uploads"

tar czf "$BACKUP_DIR/bindex-$STAMP.tar.gz" -C "$WORK" stock.db uploads

find "$BACKUP_DIR" -name 'bindex-*.tar.gz' -mtime "+$KEEP_DAYS" -delete

# Push today's backup to Mega (remote name "mega", set up via
# `rclone config`). Old copies up there are left alone — clean those up
# from Mega's own trash/retention if you want, this script only prunes
# locally.
if command -v rclone >/dev/null && rclone listremotes | grep -q '^mega:'; then
    # Remote folder keeps the old name deliberately: renaming it would
    # start a fresh folder and strand the backups already up there.
    rclone copy "$BACKUP_DIR/bindex-$STAMP.tar.gz" mega:Bindex-Backups/
fi
