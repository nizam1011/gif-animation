#!/usr/bin/env bash
# Replaces the live database with a backup made by backup.sh. Everything since that backup is lost.
# Usage: deploy/restore.sh backups/app-YYYYMMDD-HHMMSS.db.gz
set -euo pipefail

CONTAINER="${CONTAINER:-shape-to-equations}"
[ $# -eq 1 ] && [ -f "$1" ] || { echo "Usage: $0 <backup .db.gz file>" >&2; exit 1; }
file="$(realpath "$1")"
gzip -t "$file"
image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"

docker stop "$CONTAINER" >/dev/null
# A helper container writes into the same volume. Leftover journal files from the old
# database must go, or SQLite would try to replay them onto the restored one.
docker run --rm --volumes-from "$CONTAINER" -v "$(dirname "$file")":/restore:ro --entrypoint sh "$image" -c \
  "rm -f /data/app.db-wal /data/app.db-shm && gunzip -c '/restore/$(basename "$file")' > /data/app.db"
docker start "$CONTAINER" >/dev/null
echo "Restored $file into $CONTAINER"
