#!/bin/bash
# SessionStart hook — Kahla House
#
# Installs the Python deps both projects use, then plumbs Supabase
# credentials through to the shell env so the in-chat handicapper flow
# (kahla-scanner/scripts/handicapper.py) can read book_snapshots.
#
# How secrets get in:
#   • SUPABASE_URL + SUPABASE_SERVICE_KEY must be set as Claude Code
#     "secrets" in the Claude Code Web settings UI. They're then exposed
#     as env vars to the sandbox at session start. This hook simply
#     re-exports them via $CLAUDE_ENV_FILE so subsequent shells (and
#     `python -m scripts.handicapper`) see them.
#   • If the secrets aren't set, the hook still installs deps and exits
#     cleanly — the website at thekahlahouse.com/handicapper still works
#     since it reads its env from Vercel, not from this sandbox.
#
# Idempotent: pip install <already-installed> is a no-op fast path.
# Web-only: no-ops on local sessions where the user already has their
# .env populated.

set -euo pipefail

# Local sessions (laptop) already have .env / venv set up — only run the
# install + secret-plumbing on Claude Code Web sandboxes.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Install only kahla-scanner Python deps. The Flask app runs on Vercel —
# it doesn't execute in this sandbox, so its requirements.txt is skipped
# to avoid PyJWT-vs-system-Python conflicts (firebase-admin pulls in a
# PyJWT version that can't cleanly upgrade the Debian-installed copy).
# The dossier CLI (`python -m scripts.handicapper`) only needs the
# scanner subproject's deps: httpx, supabase, python-dotenv, rapidfuzz.
pip install -q --user -r "$CLAUDE_PROJECT_DIR/kahla-scanner/requirements.txt"

# Propagate runtime credentials. Claude Code Web injects user-configured
# secrets as env vars; we just need to re-export them via $CLAUDE_ENV_FILE
# so every subsequent shell (and the handicapper CLI) sees them.
#
# Required for in-chat handicapper flow:
#   • SUPABASE_URL + SUPABASE_SERVICE_KEY — read book_snapshots, write
#     bot_picks. Without these the dossier CLI can't run.
#   • ODDS_API_KEY (optional) — enables live odds fetch in the in-chat
#     CLI flow, mirroring the website's "Pick" button. Without it the
#     CLI falls back to cached snapshots (still works, just less fresh).
if [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_KEY:-}" ]; then
  echo "export SUPABASE_URL=\"${SUPABASE_URL}\""           >> "$CLAUDE_ENV_FILE"
  echo "export SUPABASE_SERVICE_KEY=\"${SUPABASE_SERVICE_KEY}\"" >> "$CLAUDE_ENV_FILE"
  echo "[session-start] Supabase creds plumbed through" >&2
else
  echo "[session-start] WARN: SUPABASE_URL / SUPABASE_SERVICE_KEY not set." >&2
  echo "[session-start] Add them as Claude Code secrets to enable the in-chat handicapper flow." >&2
  echo "[session-start] (Website at /handicapper still works — it reads env from Vercel.)" >&2
fi

if [ -n "${ODDS_API_KEY:-}" ]; then
  echo "export ODDS_API_KEY=\"${ODDS_API_KEY}\"" >> "$CLAUDE_ENV_FILE"
  echo "[session-start] ODDS_API_KEY plumbed through (in-chat live odds enabled)" >&2
else
  echo "[session-start] note: ODDS_API_KEY not set — in-chat handicapper will use cached odds only." >&2
fi

# Also write PYTHONPATH so `python -m scripts.handicapper` works from any
# CWD — the kahla-scanner subproject expects to be invoked from inside
# its own dir, but adding it to PYTHONPATH lets us run from anywhere.
echo "export PYTHONPATH=\"\${PYTHONPATH:+\$PYTHONPATH:}$CLAUDE_PROJECT_DIR/kahla-scanner\"" >> "$CLAUDE_ENV_FILE"
