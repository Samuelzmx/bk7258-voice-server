#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found."
  echo "Download Python from: https://www.python.org/downloads/macos/"
  exit 1
fi

python3 -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo ""
echo "Setup complete."
echo "Next:"
echo "1. Create .env from .env.example"
echo "2. Put in DEEPGRAM_API_KEY and ANTHROPIC_API_KEY"
echo "3. Double-click start_server.command"
