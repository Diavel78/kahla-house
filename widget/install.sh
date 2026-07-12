#!/bin/bash
# Kahla House Pick Bot widget — one-command installer for macOS.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Diavel78/kahla-house/main/widget/install.sh)
#
# Does everything: installs SwiftBar (brew if present, else direct
# download), picks/creates a plugin folder, downloads the plugin,
# prompts once for the ticker secret, verifies it against the live
# endpoint, and launches SwiftBar.
set -euo pipefail

BASE_URL="https://thekahlahouse.com"
RAW="https://raw.githubusercontent.com/Diavel78/kahla-house/main/widget"
CFG_DIR="$HOME/.config/kahla"
CFG="$CFG_DIR/widget.env"

echo "── Kahla House Pick Bot widget installer ──"

# 1. SwiftBar ---------------------------------------------------------
if [ ! -d "/Applications/SwiftBar.app" ]; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing SwiftBar via Homebrew…"
        brew install --cask swiftbar
    else
        echo "Downloading SwiftBar…"
        # Release assets are version-stamped (SwiftBar.v2.0.1.b536.zip) —
        # no stable "latest" filename, so resolve it via the GitHub API.
        DL="$(curl -fsSL https://api.github.com/repos/swiftbar/SwiftBar/releases/latest \
              | sed -n 's/.*"browser_download_url": *"\([^"]*\.zip\)".*/\1/p' | head -1)"
        if [ -z "$DL" ]; then
            DL="https://github.com/swiftbar/SwiftBar/releases/download/v2.0.1/SwiftBar.v2.0.1.b536.zip"
        fi
        TMP="$(mktemp -d)"
        curl -fsSL -o "$TMP/SwiftBar.zip" "$DL"
        unzip -q "$TMP/SwiftBar.zip" -d "$TMP"
        APP="$(find "$TMP" -maxdepth 2 -name 'SwiftBar.app' | head -1)"
        if [ -z "$APP" ]; then
            echo "Couldn't find SwiftBar.app inside the download — aborting."
            exit 1
        fi
        mv "$APP" /Applications/
        rm -rf "$TMP"
    fi
else
    echo "SwiftBar already installed ✓"
fi

# 2. Plugin folder ----------------------------------------------------
# Reuse an existing SwiftBar plugin dir if one is configured; otherwise
# set ~/.swiftbar so first launch doesn't stop to ask.
PLUG="$(defaults read com.ameba.SwiftBar PluginDirectoryPath 2>/dev/null \
     || defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null \
     || echo "")"
if [ -z "$PLUG" ]; then
    PLUG="$HOME/.swiftbar"
    defaults write com.ameba.SwiftBar PluginDirectoryPath -string "$PLUG"
    defaults write com.ameba.SwiftBar PluginDirectory -string "$PLUG"
fi
PLUG="${PLUG/#\~/$HOME}"
mkdir -p "$PLUG"
echo "Plugin folder: $PLUG"

# 3. The plugin itself ------------------------------------------------
# Prefer the copy sitting next to this script (running from the cloned
# repo) — raw.githubusercontent caches for minutes and rate-limits, so
# only curl when running via the one-liner outside the repo.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/pickbot.60s.py" ]; then
    cp "$SELF_DIR/pickbot.60s.py" "$PLUG/pickbot.60s.py"
else
    curl -fsSL -o "$PLUG/pickbot.60s.py" "$RAW/pickbot.60s.py"
fi
chmod +x "$PLUG/pickbot.60s.py"
echo "Plugin installed ✓"

# 4. Secret -----------------------------------------------------------
KEY=""
if [ -f "$CFG" ]; then
    KEY="$(sed -n 's/^KAHLA_TICKER_KEY=//p' "$CFG" | head -1)"
fi
if [ -z "$KEY" ]; then
    echo ""
    echo "Paste your ticker secret. It's the FILLS_CRON_SECRET value —"
    echo "source: Vercel → kahla-house → Settings → Env Vars (or GitHub"
    echo "repo secrets / any fill_ping curl URL's ?key=THIS-PART)."
    read -r -p "Secret: " KEY
    mkdir -p "$CFG_DIR"
    printf 'KAHLA_TICKER_KEY=%s\n' "$KEY" > "$CFG"
    chmod 600 "$CFG"
    echo "Secret stored in $CFG ✓"
else
    echo "Secret already configured ✓"
fi

# 5. Verify against the live endpoint ---------------------------------
echo "Testing the feed…"
if curl -fsS "$BASE_URL/api/handicapper/ticker?key=$KEY" 2>/dev/null | grep -q '"ok":\s*true'; then
    echo "Feed OK ✓"
else
    echo "⚠️  The endpoint didn't accept that key (or the site is mid-deploy)."
    echo "   The widget will show PB ⚠︎ until the key is right — re-run this"
    echo "   installer to re-enter it. Continuing anyway."
fi

# 6. Launch -----------------------------------------------------------
open -a SwiftBar
echo ""
echo "Done. Look for ⚡/✓ units in the menu bar (may take ~60s for the"
echo "first refresh). Click it for per-bet detail."
