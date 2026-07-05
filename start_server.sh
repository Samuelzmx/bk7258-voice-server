#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:${DYLD_LIBRARY_PATH:-}"

have_command() {
  command -v "$1" >/dev/null 2>&1
}

detect_local_ip() {
  python3 - <<'PY'
import socket
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        print(sock.getsockname()[0])
except OSError:
    print("127.0.0.1")
PY
}

create_venv() {
  if have_command uv; then
    if ! uv venv --python python3.14 >/dev/null 2>&1; then
      uv venv >/dev/null
    fi
  else
    python3 -m venv .venv
  fi
}

runtime_packages_ready() {
  .venv/bin/python3 - <<'PY'
import importlib.util
mods = ("opuslib", "requests", "dotenv", "loguru")
missing = [name for name in mods if importlib.util.find_spec(name) is None]
raise SystemExit(1 if missing else 0)
PY
}

install_runtime_packages() {
  if have_command uv; then
    uv pip install --python .venv/bin/python3 opuslib requests python-dotenv loguru
  else
    .venv/bin/python3 -m pip install --upgrade pip
    .venv/bin/python3 -m pip install opuslib requests python-dotenv loguru
  fi
}

if [ ! -f .env ]; then
  echo "ERROR: .env file not found."
  echo "Run ./setup_server.command first, then start the server again."
  exit 1
fi

if grep -q "your_deepgram_api_key_here" .env || grep -q "your_anthropic_api_key_here" .env; then
  echo "ERROR: .env still has placeholder API keys."
  echo "Open .env, paste real DEEPGRAM_API_KEY and ANTHROPIC_API_KEY values, save, then run again."
  exit 1
fi

if [ ! -f .venv/bin/python3 ]; then
  echo "Creating Python virtual environment..."
  create_venv
fi

if ! runtime_packages_ready; then
  echo "Installing Python runtime packages..."
  install_runtime_packages
fi

LOCAL_IP="$(detect_local_ip)"
echo "Starting Dawn voice server on ws://${LOCAL_IP}:8765"
echo "Control panel will be available at http://${LOCAL_IP}:8766/"
.venv/bin/python3 wss_server.py
