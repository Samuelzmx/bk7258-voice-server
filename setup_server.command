#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN=""
if command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.13)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_VERSION="$(python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')"
  if [ "$PYTHON_VERSION" = "3.13" ]; then
    PYTHON_BIN="$(command -v python3)"
  fi
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.13 is required."
  echo "Install Python 3.13 from: https://www.python.org/downloads/macos/"
  echo "Then run this setup file again."
  exit 1
fi

if [ -d .venv ]; then
  echo "Removing old .venv so setup starts clean..."
  rm -rf .venv
fi

"$PYTHON_BIN" -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python3 -c "import ast, pathlib; ast.parse(pathlib.Path('wss_server.py').read_text()); print('Server syntax check: OK')"

echo ""
echo "Setup complete."
echo "Next:"
echo "1. Create .env from .env.example"
echo "2. Put in DEEPGRAM_API_KEY and ANTHROPIC_API_KEY"
echo "3. Double-click start_server.command"
