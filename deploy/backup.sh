#!/usr/bin/env bash
# Saves a dated, compressed copy of the Shape to Equations database.
# Meant to be run nightly from cron; see README. Override with env: CONTAINER, DEST, KEEP_DAYS.
set -euo pipefail

CONTAINER="${CONTAINER:-shape-to-equations}"
DEST="${DEST:-$(cd "$(dirname "$0")/.." && pwd)/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

mkdir -p "$DEST"
chmod 700 "$DEST"   # the database holds password hashes and the mail password
out="$DEST/app-$(date +%Y%m%d-%H%M%S).db"

# SQLite's own backup gives a consistent copy even while the app is writing.
docker exec "$CONTAINER" python -c "
import sqlite3
src, dst = sqlite3.connect('/data/app.db'), sqlite3.connect('/tmp/backup.db')
with dst:
    src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'backup failed its integrity check'
dst.close(); src.close()
"
docker cp -q "$CONTAINER:/tmp/backup.db" "$out"
docker exec "$CONTAINER" rm -f /tmp/backup.db
docker cp -q "$CONTAINER:/data/secret_key" "$DEST/secret_key"
gzip "$out"
chmod 600 "$out.gz" "$DEST/secret_key"

find "$DEST" -name 'app-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) saved $out.gz ($(du -h "$out.gz" | cut -f1)), $(find "$DEST" -name 'app-*.db.gz' | wc -l) backups kept"
