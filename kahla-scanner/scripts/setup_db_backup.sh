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
if ! command -v brew >/dev/null; then
  echo "Homebrew not found — install it first (https://brew.sh)"; exit 1
fi
brew list postgresql@17 >/dev/null 2>&1 || brew install postgresql@17
brew services start postgresql@17 >/dev/null
for p in /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@17/bin; do
  [ -d "$p" ] && PATH="$p:$PATH"
done
export PATH
# give the server a moment on first boot
for i in 1 2 3 4 5 6 7 8 9 10; do
  psql -d postgres -c 'select 1' >/dev/null 2>&1 && break
  sleep 2
done
psql -d postgres -c 'select 1' >/dev/null 2>&1 \
  && echo "   postgres up ✓" || { echo "   postgres did not come up"; exit 1; }

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
bash kahla-scanner/scripts/db_backup.sh \
  && echo "ALL DONE — dump + shadow restore verified. The daily check watches it from here." \
  || { echo "Backup #1 FAILED — read the message above (most likely the connection string)."; exit 1; }
