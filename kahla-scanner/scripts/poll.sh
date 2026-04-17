#!/usr/bin/env bash
# Local equivalent of .github/workflows/scanner-poll.yml.
# Runs the full poll pipeline once against kahla-scanner/.env. Each step is
# independent — failures don't abort the rest (matches continue-on-error in CI).
#
# Usage (from anywhere):
#   ./kahla-scanner/scripts/poll.sh
#   ./kahla-scanner/scripts/poll.sh --sports NFL,MLB --days 5
#   ./kahla-scanner/scripts/poll.sh --skip dk,fd        # skip individual steps
#
# Steps, in order:
#   1. discover            scrapers.discover --sports ... --days ...
#   2. autoseed            scrapers.polymarket autoseed
#   3. poll                scrapers.polymarket poll
#   4. dk   (per sport)    scrapers.draftkings scrape <sport>
#   5. fd   (per sport)    scrapers.fanduel   scrape <sport>
#   6. resolve             analytics.resolve
#
# Requires: .env filled out, venv at kahla-scanner/venv (create with setup.sh).

set -u

# Run from the kahla-scanner/ directory regardless of where the script was
# invoked. The Python modules expect that as the cwd.
cd "$(dirname "$0")/.."

DISCOVER_SPORTS="MLB,NBA,NHL"
DISCOVER_DAYS="3"
SKIP=""

while [ $# -gt 0 ]; do
    case "$1" in
        --sports) DISCOVER_SPORTS="$2"; shift 2;;
        --days)   DISCOVER_DAYS="$2";   shift 2;;
        --skip)   SKIP="$2";            shift 2;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

if [ -f venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

if [ ! -f .env ]; then
    echo "ERROR: kahla-scanner/.env not found. Run scripts/setup.sh first." >&2
    exit 1
fi

# config.py reads .env via python-dotenv, but the shell loop below also needs
# SPORTS_ENABLED for DK/FD iteration.
set -a
# shellcheck disable=SC1091
source .env
set +a
: "${SPORTS_ENABLED:=NFL,NBA,MLB,NHL,CBB}"

skip() { [[ ",${SKIP}," == *",$1,"* ]]; }

step() {
    local tag="$1" name="$2"; shift 2
    if skip "$tag"; then
        echo
        echo "---- [skip] $name"
        return
    fi
    echo
    echo "========================================"
    echo "  $name"
    echo "========================================"
    local start=$SECONDS
    "$@" || echo "  [!] $name exited $?"
    echo "  [${tag}: $((SECONDS - start))s]"
}

step discover "Discover new markets (${DISCOVER_SPORTS}, ${DISCOVER_DAYS}d)" \
    python -m scrapers.discover --sports "$DISCOVER_SPORTS" --days "$DISCOVER_DAYS"

step autoseed "Auto-seed from Poly positions" \
    python -m scrapers.polymarket autoseed

step poll "Poll Polymarket BBO" \
    python -m scrapers.polymarket poll

IFS=',' read -ra SPORTS <<< "$SPORTS_ENABLED"
for s in "${SPORTS[@]}"; do
    step dk "DK scrape: $s" python -m scrapers.draftkings scrape "$s"
done
for s in "${SPORTS[@]}"; do
    step fd "FD scrape: $s" python -m scrapers.fanduel scrape "$s"
done

step resolve "Resolve finished games (ESPN)" \
    python -m analytics.resolve

echo
echo "========================================"
echo "  Poll complete"
echo "========================================"
