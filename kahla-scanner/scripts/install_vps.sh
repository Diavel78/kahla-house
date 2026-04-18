#!/usr/bin/env bash
# One-shot installer for Kahla Scanner on a fresh Ubuntu VPS
# (DigitalOcean / Hetzner / Vultr / Lightsail — tested on Ubuntu 24.04).
#
# Run on the VPS as root after SSH'ing in:
#
#   curl -fsSL https://raw.githubusercontent.com/Diavel78/kahla-house/main/kahla-scanner/scripts/install_vps.sh | sudo bash
#
# What it does:
#   1. Installs Python 3.11, git, build deps.
#   2. Creates a `scanner` system user.
#   3. Clones kahla-house into /opt/kahla-scanner.
#   4. Creates a venv + installs requirements.
#   5. Interactively asks for the 4 required env vars (SUPABASE_URL,
#      SUPABASE_SERVICE_KEY, POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY),
#      writes them into /opt/kahla-scanner/.env.
#   6. Installs + starts the systemd unit — `python main.py` (APScheduler),
#      polling Poly every 45s, DK/FD every 3min, ESPN resolver hourly.
#   7. Enables auto-start on boot.
#
# Uninstall:
#   systemctl stop kahla-scanner && systemctl disable kahla-scanner
#   rm /etc/systemd/system/kahla-scanner.service
#   rm -rf /opt/kahla-scanner
#   userdel -r scanner

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Run as root (sudo bash ...)." >&2
  exit 1
fi

REPO="https://github.com/Diavel78/kahla-house.git"
BRANCH="${KAHLA_BRANCH:-main}"
INSTALL_DIR="/opt/kahla-scanner"
SCANNER_USER="scanner"

echo "== 1/7  installing apt deps =="
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git python3.12 python3.12-venv python3-pip build-essential \
  libssl-dev libffi-dev curl

echo "== 2/7  ensuring 2GB swap (pip build OOMs on 512MB droplets otherwise) =="
if ! swapon --show | grep -q '^/swapfile'; then
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
fi
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

echo "== 3/7  creating scanner user =="
if ! id -u "$SCANNER_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$SCANNER_USER"
fi

echo "== 4/7  cloning repo to $INSTALL_DIR =="
mkdir -p /opt
if [ -d "$INSTALL_DIR/.git" ]; then
  (cd "$INSTALL_DIR" && sudo -u "$SCANNER_USER" git fetch --all \
                    && sudo -u "$SCANNER_USER" git checkout "$BRANCH" \
                    && sudo -u "$SCANNER_USER" git pull)
else
  rm -rf "$INSTALL_DIR"
  # Clone as root (scanner user can't write to /opt), then hand ownership over.
  git clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
  chown -R "$SCANNER_USER:$SCANNER_USER" "$INSTALL_DIR"
fi

# The Flask app lives at repo root, the scanner is the subdir we care about.
SCANNER_DIR="$INSTALL_DIR/kahla-scanner"

echo "== 5/7  creating venv + installing requirements =="
cd "$SCANNER_DIR"
if [ ! -d venv ]; then
  sudo -u "$SCANNER_USER" python3.12 -m venv venv
fi
sudo -u "$SCANNER_USER" ./venv/bin/pip install --quiet --upgrade pip
sudo -u "$SCANNER_USER" ./venv/bin/pip install --quiet -r requirements.txt

echo "== 6/7  configuring .env =="
ENV_FILE="$SCANNER_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"

  echo
  echo ">>> Enter the 4 required credentials (get them from Vercel -> kahla-house -> Settings -> Environment Variables):"
  # Read from /dev/tty so `curl ... | bash` still works (otherwise reads eat script lines).
  read -p "  SUPABASE_URL:           " SUPABASE_URL        </dev/tty
  read -p "  SUPABASE_SERVICE_KEY:   " SUPABASE_SERVICE_KEY </dev/tty
  read -p "  POLYMARKET_KEY_ID:      " POLY_KEY_ID         </dev/tty
  read -p "  POLYMARKET_SECRET_KEY:  " POLY_SECRET         </dev/tty

  # Set SPORTS_ENABLED to a reasonable default + ensure all 4 creds are set.
  python3 - "$ENV_FILE" <<PYEOF
import sys, re
path = sys.argv[1]
kvs = {
    'SUPABASE_URL':          "$SUPABASE_URL",
    'SUPABASE_SERVICE_KEY':  "$SUPABASE_SERVICE_KEY",
    'POLYMARKET_KEY_ID':     "$POLY_KEY_ID",
    'POLYMARKET_SECRET_KEY': "$POLY_SECRET",
    'SPORTS_ENABLED':        'NFL,NBA,MLB,NHL,CBB',
}
with open(path) as f:
    lines = f.readlines()
out = []
seen = set()
for line in lines:
    m = re.match(r'^\s*([A-Z_]+)\s*=', line)
    if m and m.group(1) in kvs:
        out.append(f"{m.group(1)}={kvs[m.group(1)]}\n")
        seen.add(m.group(1))
    else:
        out.append(line)
for k, v in kvs.items():
    if k not in seen:
        out.append(f"{k}={v}\n")
with open(path, "w") as f:
    f.writelines(out)
PYEOF
  chown "$SCANNER_USER:$SCANNER_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  echo "  (.env already exists — leaving alone)"
fi

echo "== 7/7  installing + starting systemd unit =="
# Rewrite WorkingDirectory in the unit to match where we actually cloned.
UNIT_SRC="$SCANNER_DIR/systemd/kahla-scanner.service"
UNIT_DST="/etc/systemd/system/kahla-scanner.service"
sed -e "s|/opt/kahla-scanner|$SCANNER_DIR|g" "$UNIT_SRC" > "$UNIT_DST"

systemctl daemon-reload
systemctl enable --now kahla-scanner

sleep 2
echo
echo "== done =="
systemctl --no-pager --full status kahla-scanner | head -n 20
echo
echo "Live logs:   journalctl -u kahla-scanner -f"
echo "Restart:     systemctl restart kahla-scanner"
echo "Stop:        systemctl stop kahla-scanner"
