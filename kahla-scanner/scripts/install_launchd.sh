#!/usr/bin/env bash
# Install the Kahla Scanner poll as a macOS launchd agent. Runs
# ./scripts/poll.sh every 30 minutes in the background, survives logout +
# reboot, no Terminal window required.
#
# Usage:
#   ./scripts/install_launchd.sh
#
# Uninstall:
#   launchctl bootout gui/$(id -u)/com.kahla.scanner-poll
#   rm ~/Library/LaunchAgents/com.kahla.scanner-poll.plist

set -eu
cd "$(dirname "$0")/.."

SCANNER_DIR="$(pwd)"
POLL_SH="$SCANNER_DIR/scripts/poll.sh"
LOG_DIR="$SCANNER_DIR/logs"
PLIST="$HOME/Library/LaunchAgents/com.kahla.scanner-poll.plist"
LABEL="com.kahla.scanner-poll"

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${POLL_SH}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${SCANNER_DIR}</string>

  <!-- Run every 1800 seconds (30 min). -->
  <key>StartInterval</key>
  <integer>1800</integer>

  <!-- Also run immediately on load. -->
  <key>RunAtLoad</key>
  <true/>

  <!-- Prevent launchd from killing runs that exceed the interval. -->
  <key>ThrottleInterval</key>
  <integer>60</integer>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/poll.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/poll.err.log</string>

  <!-- Make sure PATH is sane; launchd's default PATH is minimal. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
EOF

echo "Wrote $PLIST"

# Unload first in case an older version is already loaded, then load fresh.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo
echo "Scanner poll is now running in the background every 30 min."
echo "Logs:  tail -f $LOG_DIR/poll.out.log"
echo "Stop:  launchctl bootout gui/\$(id -u)/${LABEL}"
echo "      rm $PLIST"
