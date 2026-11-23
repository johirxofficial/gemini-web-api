#!/usr/bin/env bash
# Gemini Web API - Linux/macOS installer
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "[2/3] Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f "config.yaml" ]; then
  echo "[3/3] Creating config.yaml..."
  cp config.example.yaml config.yaml
else
  echo "[3/3] config.yaml already exists - keeping it."
fi

chmod +x api.sh login.sh ask.sh doctor.sh 2>/dev/null || true

echo
echo "✅ Install complete."
echo "   Start the API : ./api.sh"
echo "   Gemini login  : ./login.sh"
echo "   Diagnostics   : ./doctor.sh"
