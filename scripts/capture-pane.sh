#!/usr/bin/env bash
# Capture a terminal pane to the parser corpus.
#
# Usage:
#   ./scripts/capture-pane.sh <session>              # tmux (default)
#   ./scripts/capture-pane.sh --wezterm <pane-id>
#   ./scripts/capture-pane.sh --tmux <session>

set -euo pipefail

backend="tmux"
session=""

for arg in "$@"; do
    case "$arg" in
        --tmux)    backend="tmux" ;;
        --wezterm) backend="wezterm" ;;
        *)         session="$arg" ;;
    esac
done

[ -n "$session" ] || { echo "Usage: capture-pane.sh [--tmux|--wezterm] <session>" >&2; exit 1; }

ts=$(date +%Y%m%d-%H%M%S)
dir="$(cd "$(dirname "$0")/../tests/plugins/claude_code/corpus/captures" && pwd)"
mkdir -p "$dir"

if [ "$backend" = "wezterm" ]; then
    wezterm cli get-text --pane-id "$session" > "$dir/${ts}.txt"
else
    tmux capture-pane -t "$session" -p -e > "$dir/${ts}.raw"
    tmux capture-pane -t "$session" -p    > "$dir/${ts}.txt"
fi

echo "$dir/${ts}.txt"
