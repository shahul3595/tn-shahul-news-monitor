#!/usr/bin/env bash
# The database between GitHub runs.
#
# Every run starts on a blank machine, so corpus.db is carried from one run to
# the next as a run artifact: compressed, then encrypted with STATE_PASSPHRASE.
# The repository is public and anyone signed in to GitHub can download run
# artifacts, which is why the file is encrypted.
#
#   state.sh restore   fetch the newest saved database (or start fresh)
#   state.sh pack      compress + encrypt corpus.db into state/state.enc
#
# To open a downloaded state.enc on your own PC (needs openssl, e.g. Git Bash):
#   openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 200000 -in state.enc -out corpus.db.gz
#   gunzip corpus.db.gz            (or open the .gz with 7-Zip)
set -euo pipefail

: "${STATE_PASSPHRASE:?STATE_PASSPHRASE is not set}"
: "${GH_TOKEN:?GH_TOKEN is not set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"

DB="corpus.db"
OUT="state"
ENC="$OUT/state.enc"

newest_artifact_id() {
  # id of the newest non-expired artifact with this name, or empty
  gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=$1&per_page=30" \
    --jq '[.artifacts[] | select(.expired == false)] | sort_by(.created_at) | reverse | .[0].id // empty'
}

restore() {
  rm -f "$DB" "$DB-wal" "$DB-shm"
  mkdir -p "$OUT"
  local id name
  for name in state state-backup; do
    id="$(newest_artifact_id "$name")"
    if [ -n "$id" ]; then
      echo "restoring database from artifact '$name' #$id"
      gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$id/zip" > "$OUT/download.zip"
      unzip -o -q "$OUT/download.zip" -d "$OUT"
      if ! openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 200000 -salt \
            -pass env:STATE_PASSPHRASE -in "$ENC" -out "$OUT/corpus.db.gz"; then
        echo "::error::Could not decrypt the saved database. STATE_PASSPHRASE has changed since it was saved."
        echo "::error::Either restore the old passphrase, or delete the 'state' and 'state-backup' artifacts to start fresh."
        exit 1
      fi
      gunzip -f "$OUT/corpus.db.gz"
      mv "$OUT/corpus.db" "$DB"
      rm -f "$OUT/download.zip" "$ENC"
      echo "database restored: $(du -h "$DB" | cut -f1)"
      return 0
    fi
  done
  echo "no saved database found -- starting fresh (normal on the very first run)"
}

pack() {
  [ -f "$DB" ] || { echo "::error::no $DB to save"; exit 1; }
  mkdir -p "$OUT"
  python3 - <<'EOF'
import sqlite3
con = sqlite3.connect("corpus.db")
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.close()
EOF
  rm -f "$DB-wal" "$DB-shm"
  gzip -c -6 "$DB" > "$OUT/corpus.db.gz"
  openssl enc -aes-256-cbc -md sha256 -pbkdf2 -iter 200000 -salt \
    -pass env:STATE_PASSPHRASE -in "$OUT/corpus.db.gz" -out "$ENC"
  rm -f "$OUT/corpus.db.gz"
  echo "database packed: $(du -h "$DB" | cut -f1) -> $(du -h "$ENC" | cut -f1) encrypted"
}

backup_due() {
  # true when no 'state-backup' artifact was made in the last 20 hours
  local last
  last="$(gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=state-backup&per_page=5" \
          --jq '[.artifacts[] | select(.expired == false) | .created_at] | sort | reverse | .[0] // empty')"
  if [ -z "$last" ]; then
    return 0
  fi
  local then now
  then="$(date -u -d "$last" +%s)"
  now="$(date -u +%s)"
  [ $((now - then)) -gt $((20 * 3600)) ]
}

case "${1:-}" in
  restore) restore ;;
  pack) pack ;;
  backup-due) if backup_due; then echo "yes"; else echo "no"; fi ;;
  *) echo "usage: state.sh restore | pack | backup-due"; exit 2 ;;
esac
