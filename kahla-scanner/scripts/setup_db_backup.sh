#!/bin/bash
# ONE-SHOT setup for the nightly DB backup shadow (run ON THE BOX, once).
# Does everything db_backup.sh's header describes: installs/starts local
# Postgres, stores the connection string, installs the launchd job, and
# fires backup #1 immediately so the first stamp lands while you watch.
#
#   cd ~/dev/kahla-house && bash kahla-scanner/scripts/setup_db_backup.sh
#
# You'll be asked to paste ONE thing: the SESSION POOLER connection string
# from Supabase (dashboard -> Connect -> Session pooler), which looks like
#   postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres
# It is written only to ~/.kahla/db_url (chmod 600), never to the repo.
set -uo pipefail
cd "$(dirname "$0")/../.."          # repo root, wherever the checkout lives

echo "== 1/4 local Postgres =="
# Postgres.app FIRST — this box runs macOS 13, which Homebrew dropped (the
# postgresql@17 source build died Aug 29 after a 40-min compile). Download
# https://postgresapp.com → drag to Applications → open it → "Initialize".
for p in /Applications/Postgres.app/Contents/Versions/latest/bin \
         /Applications/Postgres.app/Contents/Versions/17/bin \
         /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@17/bin; do
  [ -d "$p" ] && PATH="$p:$PATH"
done
export PATH
if ! command -v pg_dump >/dev/null; then
  echo "   No Postgres tools found. On this Mac (macOS 13) use Postgres.app:"
  echo "   1. Download https://postgresapp.com (latest with PostgreSQL 17)"
  echo "   2. Drag to Applications, open it, click 'Initialize'"
  echo "   3. In Postgres.app preferences: enable 'Start at login'"
  echo "   4. Re-run this script."
  exit 1
fi
# give the server a moment
for i in 1 2 3 4 5 6 7 8 9 10; do
  psql -d postgres -c 'select 1' >/dev/null 2>&1 && break
  sleep 2
done
if psql -d postgres -c 'select 1' >/dev/null 2>&1; then
  echo "   postgres up ✓ ($(command -v psql))"
else
  echo "   pg tools found but no server running — open Postgres.app and"
  echo "   click Start (and enable 'Start at login'), then re-run."
  echo "   (Continuing anyway: dumps still work without the local server —"
  echo "    the shadow-restore test just gets skipped until it's up.)"
fi

echo "== 2/4 connection string =="
mkdir -p "$HOME/.kahla" && chmod 700 "$HOME/.kahla"
if [ -s "$HOME/.kahla/db_url" ]; then
  echo "   ~/.kahla/db_url already exists — keeping it ✓"
else
  printf "   Paste the Session pooler connection string and hit enter:\n   > "
  IFS= read -r DBURL
  case "$DBURL" in
    postgresql://*pooler.supabase.com*) : ;;
    postgresql://*|postgres://*)
      echo "   (warning: that doesn't look like the pooler host — the direct"
      echo "    db.<ref> host is IPv6-only and may not connect. Continuing.)" ;;
    *) echo "   That doesn't look like a postgres URL — aborting, nothing saved."
       exit 1 ;;
  esac
  printf '%s\n' "$DBURL" > "$HOME/.kahla/db_url" && chmod 600 "$HOME/.kahla/db_url"
  echo "   saved ✓ (chmod 600)"
fi

echo "== 3/4 launchd nightly job (3:30am AZ) =="
sudo cp cellar/com.kahla.dbbackup.plist /Library/LaunchDaemons/
sudo launchctl unload /Library/LaunchDaemons/com.kahla.dbbackup.plist 2>/dev/null
sudo launchctl load /Library/LaunchDaemons/com.kahla.dbbackup.plist
echo "   installed ✓"

echo "== 4/4 firing backup #1 now =="
if ! bash kahla-scanner/scripts/db_backup.sh; then
  echo "Backup #1 FAILED — read the message above (most likely the connection string)."
  exit 1
fi
# Say what actually happened — the restore test only runs when the local
# server is up, and "verified" must never print for a skipped test.
if psql -d kahla_shadow -Atc 'select 1' >/dev/null 2>&1; then
  echo "ALL DONE — dump taken AND shadow restore verified. The daily check watches it from here."
else
  echo "DUMP DONE (a real backup). Shadow-restore test SKIPPED — start Postgres.app"
  echo "and run 'bash kahla-scanner/scripts/db_backup.sh' once to verify a restore."
fi
