#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
exec ./start_server.sh
