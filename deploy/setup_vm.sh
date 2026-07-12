#!/usr/bin/env bash
# Cryptera (v3.2.4-correctness) Azure VM setup script.
#
# Run this ONCE on a fresh Ubuntu VM, from the root of the cloned repo, after your
# .env file has already been copied into place (see AZURE_DEPLOYMENT.md, step 4):
#
#   cd ~/Cryptera-v3
#   bash deploy/setup_vm.sh
#
# It installs Python + a virtualenv, installs project dependencies (including a fix for
# the pandas-ta package deletion -- see AZURE_DEPLOYMENT.md section 3), adds a swap file,
# compiles every module as a sanity check, and registers (but does not yet start) the
# systemd service that runs monitor.py 24/7.
#
# This is a SEPARATE deployment from any other Cryptera version's VM -- it does not touch,
# share state with, or depend on any other machine's install.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [ ! -f ".env" ]; then
  echo "ERROR: .env not found in $REPO_DIR -- copy it here first (see AZURE_DEPLOYMENT.md step 4)." >&2
  exit 1
fi

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip build-essential git

echo "==> Adding a 2GB swap file (safety net for the 1GB free-tier VM)"
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
else
  echo "    (swap file already present, skipping)"
fi

echo "==> Creating Python virtual environment"
python3 -m venv "$REPO_DIR/venv"
# shellcheck disable=SC1091
source "$REPO_DIR/venv/bin/activate"
pip install --upgrade pip --quiet

echo "==> Installing project dependencies"
grep -v '^pandas-ta' requirements.txt > /tmp/reqs_no_ta.txt
pip install -r /tmp/reqs_no_ta.txt --quiet

echo "==> Applying the pandas-ta fix (see AZURE_DEPLOYMENT.md section 3)"
# pandas-ta was removed from PyPI entirely by its maintainer (whole project taken down) --
# any pin (this repo's requirements.txt pins pandas-ta==0.4.71b0) fails to resolve on a
# fresh install. pandas-ta-classic is the community-maintained continuation of the same
# codebase (same functions, same math) -- confirmed here that all eight indicator
# functions indicators.py uses are present and identical: ema, atr, rsi, macd, supertrend,
# stochrsi, adx, bbands. This installs a pinned, known-good release and re-exposes it under
# the original `pandas_ta` import name, so no project source file needs to change.
pip install --quiet "pandas-ta-classic==0.3.14b2"
SITE_DIR="$(python3 -c 'import site; print(site.getsitepackages()[0])')"
mkdir -p "$SITE_DIR/pandas_ta"
cat > "$SITE_DIR/pandas_ta/__init__.py" << 'PYEOF'
"""Compatibility shim: pandas_ta was removed from PyPI upstream (project taken down).
`pandas-ta-classic` is the community-maintained continuation from the same 0.3.14
codebase. This shim re-exposes it under the original `pandas_ta` import name so
project source needs zero changes.
"""
from pandas_ta_classic import *  # noqa: F401,F403
from pandas_ta_classic import (ema, atr, rsi, macd, supertrend, stochrsi, adx, bbands)  # noqa: F401
PYEOF

echo "==> Verifying the fix"
python3 -c "import pandas_ta as ta; assert ta.ema and ta.supertrend; print('    pandas_ta OK via classic shim')"

echo "==> Compiling all project modules (confirms a clean, uncorrupted checkout)"
python3 -c "
import py_compile, glob
files = (glob.glob('*.py') + glob.glob('alerts/*.py') + glob.glob('tools/*.py')
         + glob.glob('web/*.py') + glob.glob('tests/*.py'))
for f in files:
    py_compile.compile(f, doraise=True)
print(f'    {len(files)} modules compiled cleanly')
"

echo "==> Installing the systemd service"
CURRENT_USER="$(whoami)"
sudo tee /etc/systemd/system/cryptera-monitor.service > /dev/null << SERVICEEOF
[Unit]
Description=Cryptera v3.2.4 collector + Telegram monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/venv/bin/python monitor.py --retention 0 --no-color
Restart=always
RestartSec=15
User=${CURRENT_USER}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload

echo ""
echo "==> Setup complete."
echo ""
echo "Next, run a one-off smoke test BEFORE going 24/7:"
echo "    source venv/bin/activate && python monitor.py --once"
echo ""
echo "Once that looks right (check Telegram + 'ls snapshots/'), start the always-on service:"
echo "    sudo systemctl enable --now cryptera-monitor"
echo "    sudo journalctl -u cryptera-monitor -f     # watch it live, Ctrl+C to stop watching"
