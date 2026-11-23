#!/usr/bin/env bash
# Gemini Web API - Linux/macOS one-shot prompt
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "❌ Run ./install.sh first."
  exit 1
fi
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
fi

exec .venv/bin/python -m gwa ask "$@"
