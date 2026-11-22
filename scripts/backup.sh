#!/usr/bin/env bash
# Consistent backup of the database and every stored file. Usage: scripts/backup.sh [destination-dir]
set -euo pipefail
DATA="${RESONANCE_DATA_DIR:-./data}"; DEST="${1:-./backups}"; STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DEST"
# sqlite3 .backup takes a consistent snapshot even while the server is running (WAL mode)
python3 - "$DATA/resonance.db" "$DEST/resonance-$STAMP.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
tar -czf "$DEST/objects-$STAMP.tar.gz" -C "$DATA" objects
echo "Backup written: $DEST/resonance-$STAMP.db and $DEST/objects-$STAMP.tar.gz"
echo "Restore: stop the server, copy the .db to \$RESONANCE_DATA_DIR/resonance.db, untar objects into \$RESONANCE_DATA_DIR."
