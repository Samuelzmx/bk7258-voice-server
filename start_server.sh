#!/bin/bash
set -e

cd "$(dirname "$0")"
export DYLD_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_LIBRARY_PATH

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example and fill in API keys."
  exit 1
fi

if [ ! -f .venv/bin/python3 ]; then
  echo "Setting up virtualenv..."
  uv venv --python python3.14
fi

if ! .venv/bin/python3 - <<'PY'
import importlib.util
mods = ("opuslib", "requests", "dotenv", "loguru")
missing = [name for name in mods if importlib.util.find_spec(name) is None]
raise SystemExit(1 if missing else 0)
PY
then
  echo "Installing missing runtime packages..."
  uv pip install opuslib requests python-dotenv loguru
fi

echo "Starting Dawn voice server on ws://10.0.0.62:8765"
echo "Control panel will be available at http://10.0.0.62:8766/"
.venv/bin/python3 wss_server.py
