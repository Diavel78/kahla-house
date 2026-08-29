#!/bin/bash
# NIGHTLY DB BACKUP + TESTED RESTORE — the local-Postgres shadow (Aug 28 2026).
#
# Runs ON THE HOUSE BOX. Three jobs, every night:
#   1. pg_dump the live Supabase database to ~/kahla-backups/ (14 kept).
#   2. Restore that dump into the LOCAL Postgres as `kahla_shadow` —
#      so the restore path is proven nightly, not hoped-for on cutover day.
#      (Spec §12e: repatriation is done when a restore WORKS.)
#   3. Stamp the outcome into exec_probe_runs (kind=db_backup) so the
#      daily machine check can see backup health without touching the box.
#
# Setup (one time, on the box):
#   brew install postgresql@17 && brew services start postgresql@17
#   mkdir -p ~/.kahla && chmod 700 ~/.kahla
#   # Paste the SESSION POOLER connection string from Supabase dashboard
#   # (Connect → Session pooler — the direct db.<ref> host is IPv6-only):
#   echo 'postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres' \
#     > ~/.kahla/db_url && chmod 600 ~/.kahla/db_url
#   # Install the launchd job (from the repo checkout):
#   sudo cp cellar/com.kahla.dbbackup.plist /Library/LaunchDaemons/ \
#     && sudo launchctl load /Library/LaunchDaemons/com.kahla.dbbackup.plist
#
# The password lives ONLY in ~/.kahla/db_url on the box — never in the repo.
set -uo pipefail

BACKUP_DIR="${KAHLA_BACKUP_DIR:-$HOME/kahla-backups}"
URL_FILE="${KAHLA_DB_URL_FILE:-$HOME/.kahla/db_url}"
KEEP_DAYS=14
SHADOW_DB="kahla_shadow"
STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="$BACKUP_DIR/kahla-$STAMP.dump"
mkdir -p "$BACKUP_DIR"

# Homebrew postgres tools (postgresql@17 is keg-only) ahead of any system stubs.
for p in /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@17/bin; do
  [ -d "$p" ] && PATH="$p:$PATH"
done
export PATH

fail() {
  local msg="$1"
  echo "db_backup FAILED: $msg" >&2
  stamp_result "false" "$msg" "" ""
  exit 1
}

stamp_result() {
  # stamp_result ok msg picks_count newest_snap  — best-effort, never fatal.
  local ok="$1" msg="$2" picks="$3" snap="$4"
  local sz=""
  [ -f "$DUMP" ] && sz=$(du -k "$DUMP" | cut -f1)
  psql "$DB_URL" -v ON_ERROR_STOP=0 -q >/dev/null 2>&1 <<SQL || true
insert into exec_probe_runs (params, result) values (
  '{"kind":"db_backup"}'::jsonb,
  jsonb_build_object(
    'ok', ${ok},
    'msg', '$(printf '%s' "$msg" | sed "s/'/''/g")',
    'dump', '$(basename "$DUMP")',
    'dump_kb', nullif('${sz}','')::bigint,
    'shadow_picks', nullif('${picks}','')::bigint,
    'shadow_newest_snap', nullif('${snap}',''),
    'host', '$(hostname -s)'
  ));
SQL
}

[ -r "$URL_FILE" ] || { echo "db_backup: no $URL_FILE — see setup header" >&2; exit 2; }
DB_URL="$(head -1 "$URL_FILE" | tr -d '[:space:]')"
[ -n "$DB_URL" ] || { echo "db_backup: $URL_FILE is empty" >&2; exit 2; }

command -v pg_dump >/dev/null || fail "pg_dump not on PATH (brew install postgresql@17)"

# ---- 1. dump the live cloud DB --------------------------------------------
if ! pg_dump "$DB_URL" -Fc --no-owner --no-privileges -f "$DUMP" 2>/tmp/kahla_dump_err; then
  fail "pg_dump: $(tail -c 300 /tmp/kahla_dump_err)"
fi
[ -s "$DUMP" ] || fail "dump file empty"

# ---- 2. restore into the local shadow (the nightly restore TEST) ----------
if command -v pg_restore >/dev/null && psql -d postgres -c 'select 1' >/dev/null 2>&1; then
  dropdb --if-exists "$SHADOW_DB" 2>/dev/null
  createdb "$SHADOW_DB" || fail "createdb $SHADOW_DB"
  # --no-owner/--no-privileges: cloud roles don't exist locally. Extension or
  # publication objects may warn — tolerated; the smoke test is the verdict.
  pg_restore --no-owner --no-privileges -d "$SHADOW_DB" "$DUMP" 2>/tmp/kahla_restore_err
  PICKS=$(psql -d "$SHADOW_DB" -Atc "select count(*) from bot_picks" 2>/dev/null)
  SNAP=$(psql -d "$SHADOW_DB" -Atc "select max(captured_at) from pm_snapshots" 2>/dev/null)
  if [ -z "$PICKS" ]; then
    fail "shadow restore smoke test failed: $(tail -c 300 /tmp/kahla_restore_err)"
  fi
else
  # No local server yet — the dump alone is still a real backup.
  PICKS=""; SNAP=""
  echo "db_backup: local postgres not running — dump taken, restore test skipped" >&2
fi

# ---- 3. prune + stamp ------------------------------------------------------
find "$BACKUP_DIR" -name 'kahla-*.dump' -mtime +$KEEP_DAYS -delete 2>/dev/null
stamp_result "true" "ok" "$PICKS" "$SNAP"
echo "db_backup OK: $(basename "$DUMP") ($(du -h "$DUMP" | cut -f1)) · shadow picks=$PICKS newest_snap=$SNAP"
