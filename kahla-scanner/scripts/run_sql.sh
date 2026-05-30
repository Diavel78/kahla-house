#!/bin/bash
# run_sql.sh — run arbitrary SQL against the Kahla House Supabase project
# from the Claude Code sandbox, via the Supabase Management API (HTTPS, so it
# works behind the sandbox proxy where raw Postgres port 5432 is blocked).
#
# This is the "stop hand-running migrations in the mobile SQL editor" tool.
# It does exactly what the dashboard SQL editor does — including DDL
# (CREATE TABLE / ALTER / etc.) — because the Management API query endpoint
# runs statements with full privileges.
#
# Requires (plumbed by .claude/hooks/session-start.sh from env vars):
#   • SUPABASE_ACCESS_TOKEN  — account-level personal access token (sbp_…)
#   • SUPABASE_PROJECT_REF   — project subdomain (e.g. xzzjpbervfoyaodduynb)
# And the environment's network allowlist must include api.supabase.com.
#
# Usage:
#   scripts/run_sql.sh "select count(*) from bot_picks;"      # inline SQL
#   scripts/run_sql.sh -f path/to/migration.sql               # from a file
#   echo "select 1;" | scripts/run_sql.sh                     # from stdin
#
# Output: the API's JSON response (array of result rows) on stdout. Non-zero
# exit + error body on stderr if the request fails.

set -euo pipefail

API="https://api.supabase.com/v1/projects"

if [ -z "${SUPABASE_ACCESS_TOKEN:-}" ] || [ -z "${SUPABASE_PROJECT_REF:-}" ]; then
  echo "run_sql.sh: SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF must be set." >&2
  echo "  (Add them as environment variables in the Claude Code cloud environment," >&2
  echo "   and allowlist api.supabase.com under Network access → Custom.)" >&2
  exit 2
fi

# ---- gather the SQL ---------------------------------------------------------
SQL=""
if [ "${1:-}" = "-f" ]; then
  [ -n "${2:-}" ] || { echo "run_sql.sh: -f requires a file path" >&2; exit 2; }
  [ -f "$2" ]     || { echo "run_sql.sh: file not found: $2" >&2; exit 2; }
  SQL="$(cat "$2")"
elif [ -n "${1:-}" ]; then
  SQL="$1"
elif [ ! -t 0 ]; then
  SQL="$(cat)"
fi

if [ -z "${SQL//[[:space:]]/}" ]; then
  echo "run_sql.sh: no SQL provided (pass inline, -f FILE, or via stdin)" >&2
  exit 2
fi

# ---- build the JSON body safely (jq handles all escaping) -------------------
BODY="$(jq -nc --arg q "$SQL" '{query: $q}')"

# ---- call the Management API ------------------------------------------------
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
HTTP_CODE="$(curl -sS -o "$TMP" -w "%{http_code}" \
  -X POST "$API/$SUPABASE_PROJECT_REF/database/query" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  --data "$BODY")"

if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
  echo "run_sql.sh: Management API returned HTTP $HTTP_CODE" >&2
  cat "$TMP" >&2
  echo >&2
  exit 1
fi

# Pretty-print if jq can parse it; otherwise raw.
if jq . "$TMP" 2>/dev/null; then
  :
else
  cat "$TMP"
fi
