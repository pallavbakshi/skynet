#!/usr/bin/env bash
# Thin wrapper around `agp send` for shell/orc use.
# Usage:
#   agp-send <agent_id> <task>                       # smart detach (90s sync)
#   agp-send <agent_id> <task> --detach              # fire and forget
#   agp-send <agent_id> <task> --timeout 300         # longer sync window
set -euo pipefail

exec uv run agp send "$@"
