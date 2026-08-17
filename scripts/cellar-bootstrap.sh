#!/usr/bin/env bash
# THE CELLAR — one-shot setup for the house box.
#
#   git clone https://github.com/Diavel78/kahla-house.git ~/dev/kahla-house
#   cd ~/dev/kahla-house && ./scripts/cellar-bootstrap.sh
#
# Idempotent: safe to re-run after fixing whatever it complained about.
# Installs NOTHING system-wide and touches no secrets it wasn't given.
#
# Spec: docs/cellar-migration-spec.md §4, §6

set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
FATAL=0

bold "THE CELLAR — bootstrap"
echo "  repo: $ROOT"
echo

# --------------------------------------------------------------------------
bold "1. Python"
# The codebase carries ~149 PEP 604 unions (`dict | None`), so 3.10 is a hard
# floor and macOS's stock python3 (3.9.6 on Ventura) CANNOT PARSE IT. The
# workflows pin 3.11. Find a good interpreter or stop here.
PY=""
for cand in python3.12 python3.11 python3.13 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done

if [ -z "$PY" ]; then
  bad "no Python >= 3.10 found."
  echo "        macOS ships 3.9.6, which cannot parse this codebase."
  echo "        Install 3.12 from python.org (universal2 .pkg) — NOT Homebrew;"
  echo "        Ventura is aging out of Homebrew's bottle tiers."
  FATAL=1
else
  ok "$($PY --version) at $(command -v $PY)"
fi

# --------------------------------------------------------------------------
bold "2. Virtualenv + dependencies"
if [ "$FATAL" -eq 0 ]; then
  if [ ! -d .venv ]; then
    "$PY" -m venv .venv && ok "created .venv" || { bad "venv creation failed"; FATAL=1; }
  else
    ok ".venv already present"
  fi
  if [ "$FATAL" -eq 0 ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pip install -q --upgrade pip
    # --prefer-binary is LOAD-BEARING on an Intel Mac (hit for real on the 2017
    # MBP, Aug 16 2026): recent `cryptography` releases ship macOS wheels for
    # arm64 only, so plain pip resolves to the newest version, finds no x86_64
    # wheel, and tries to COMPILE it through Rust/maturin -- which fails on a
    # box with no Rust toolchain, and would take forever on a dual-core even
    # with one. --prefer-binary makes pip fall back to the newest version that
    # actually HAS a wheel. Harmless on Apple Silicon and Linux.
    if python -m pip install -q --prefer-binary \
         -r requirements.txt -r kahla-scanner/requirements.txt; then
      ok "installed requirements.txt + kahla-scanner/requirements.txt"
    else
      bad "dependency install failed — scroll up for the offending package"
      echo "        If it is a package trying to COMPILE (maturin / cargo /"
      echo "        'building wheel for ...'), no prebuilt wheel matched this"
      echo "        platform. Try pinning that one package to a wheeled build:"
      echo "            source .venv/bin/activate"
      echo "            pip install --only-binary=:all: <package>"
      echo "        Last resort, install a compiler toolchain:"
      echo "            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
      FATAL=1
    fi
  fi
fi

# --------------------------------------------------------------------------
bold "3. Secrets"
# Never generated, never guessed. Copy the values out of the Vercel dashboard.
NEEDED=(SUPABASE_URL SUPABASE_SERVICE_KEY FIREBASE_SERVICE_ACCOUNT
        POLYMARKET_KEY_ID POLYMARKET_SECRET_KEY FLASK_SECRET_KEY
        FILLS_CRON_SECRET FILLED_BOT_TOKEN FILLED_BOT_CHAT_ID)
if [ ! -f .env ]; then
  {
    echo "# THE CELLAR — copy values from the Vercel project env, then re-run bootstrap."
    echo "# This file is gitignored. chmod 600 it."
    for k in "${NEEDED[@]}"; do echo "$k="; done
    echo
    echo "# --- cellar daemon ---"
    echo "CELLAR_DRY_RUN=1       # 1 = no money moves. Leave it until cutover."
    echo "CELLAR_LANES=          # empty = run nothing. Add one lane at a time."
  } > .env
  chmod 600 .env
  warn "wrote a blank .env — fill it in from Vercel, then re-run this script"
else
  chmod 600 .env
  MISSING=()
  for k in "${NEEDED[@]}"; do
    grep -qE "^${k}=.+" .env || MISSING+=("$k")
  done
  if [ ${#MISSING[@]} -eq 0 ]; then
    ok "all ${#NEEDED[@]} required keys present in .env (mode 600)"
  else
    warn "empty in .env: ${MISSING[*]}"
  fi
fi

# --------------------------------------------------------------------------
bold "4. Selftest"
if [ "$FATAL" -eq 0 ]; then
  python -m cellar --selftest || FATAL=1
fi

# --------------------------------------------------------------------------
bold "5. Box readiness (informational — see spec §4)"
if [ "$(uname -s)" = "Darwin" ]; then
  FV="$(fdesetup status 2>/dev/null || echo unknown)"
  case "$FV" in
    *Off*) ok "FileVault off — the box reboots unattended after a power cut" ;;
    *On*)  warn "FileVault ON — a power cut leaves the disk locked until someone" ;
           echo "        types the password. That is SURVIVABLE: the lease hands"
           echo "        the lanes back to Vercel automatically (spec §5/§10)."
           echo "        Cost is the cellar staying down until you're home." ;;
    *)     warn "FileVault status unknown" ;;
  esac
  BATT="$(system_profiler SPPowerDataType 2>/dev/null | awk '/Condition/{print $2; exit}')"
  [ -n "$BATT" ] && { [ "$BATT" = "Normal" ] && ok "battery condition: $BATT" || warn "battery condition: $BATT — replace before running this box unattended"; }
  pmset -g 2>/dev/null | grep -q "disablesleep.*1" \
    && ok "sleep disabled (clamshell-safe)" \
    || warn "run: sudo pmset -a disablesleep 1 sleep 0 disksleep 0 powernap 0 autorestart 1"
  [ "$(date +%Z)" = "MST" ] && ok "timezone MST (America/Phoenix)" \
    || warn "timezone is $(date +%Z) — set America/Phoenix (every 'today' here is an AZ day)"
else
  warn "not macOS — skipping box checks"
fi

echo
if [ "$FATAL" -ne 0 ]; then
  bold "BOOTSTRAP INCOMPLETE — fix the FAIL lines above and re-run."
  exit 1
fi
bold "Ready."
cat <<'NEXT'

  source .venv/bin/activate
  python -m cellar --status      # who owns which lane right now
  python -m cellar               # run the daemon (inert until CELLAR_LANES is set)

  Cutover is one lane at a time, in the spec's order (§6 Phase 3):
      CELLAR_LANES=pm_snapshot   <- start here. Pure logger, zero money.
NEXT
