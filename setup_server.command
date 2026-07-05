#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

have_command() {
  command -v "$1" >/dev/null 2>&1
}

say_step() {
  echo
  echo "== $1 =="
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

install_runtime_packages() {
  if have_command uv; then
    uv pip install --python .venv/bin/python3 opuslib requests python-dotenv loguru
  else
    .venv/bin/python3 -m pip install --upgrade pip
    .venv/bin/python3 -m pip install opuslib requests python-dotenv loguru
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

say_step "Checking macOS prerequisites"
if ! have_command python3; then
  echo "Python 3 is missing. Install it first, then run this setup again."
  exit 1
fi

if ! have_command brew; then
  echo "Homebrew is required to install libopus."
  echo "Install Homebrew from: https://brew.sh"
  exit 1
fi

say_step "Checking libopus"
if [ ! -f /opt/homebrew/lib/libopus.dylib ] && [ ! -f /opt/homebrew/lib/libopus.0.dylib ] && [ ! -f /usr/local/lib/libopus.dylib ] && [ ! -f /usr/local/lib/libopus.0.dylib ]; then
  echo "Installing opus with Homebrew..."
  brew install opus
else
  echo "libopus already available."
fi

say_step "Preparing API key file"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo ".env already exists."
fi
open -a TextEdit .env
echo "TextEdit opened .env."
echo "Paste DEEPGRAM_API_KEY and ANTHROPIC_API_KEY, save the file, then close TextEdit."

say_step "Preparing Python environment"
if [ ! -f .venv/bin/python3 ]; then
  echo "Creating .venv..."
  create_venv
else
  echo ".venv already exists."
fi

if ! runtime_packages_ready; then
  echo "Installing Python packages..."
  install_runtime_packages
else
  echo "Python packages already installed."
fi

LOCAL_IP="$(detect_local_ip)"
say_step "Setup complete"
echo "Next: double-click start_server.command"
echo "Server URL: ws://${LOCAL_IP}:8765"
echo "Control panel: http://${LOCAL_IP}:8766/"
