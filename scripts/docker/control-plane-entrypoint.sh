#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 && "${1}" != -* ]]; then
  exec "$@"
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  exec python -m agp serve "$@"
fi

HOST="${AGP_HOST:-0.0.0.0}"
PORT="${AGP_PORT:-7860}"

python -m agp initdb
exec python -m agp serve --host "${HOST}" --port "${PORT}" "$@"
