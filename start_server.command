#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ./.venv/bin/python3 ]; then
  echo "Missing .venv."
  echo "Run setup_server.command first."
  exit 1
fi

if [ ! -f ./.env ]; then
  echo "Missing .env."
  echo "Copy .env.example to .env and add DEEPGRAM_API_KEY and ANTHROPIC_API_KEY."
  exit 1
fi

echo "Control panel will be available at http://YOUR_MAC_IP:8766/"
exec ./.venv/bin/python3 ./wss_server.py
