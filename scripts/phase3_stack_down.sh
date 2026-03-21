#!/usr/bin/env bash
# DEPRECATED: Use `skyops down` instead.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  echo "Docker daemon is not reachable" >&2
  exit 1
fi

"${DOCKER[@]}" compose -f compose.phase3.yaml down -v --remove-orphans
