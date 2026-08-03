#!/usr/bin/env bash
# Capture a terminal pane to the smallops Claude Code parser corpus.
#
# Usage:
#   ./scripts/capture-pane.sh <category> <name> [session] [--tmux|--wezterm] [--force]
#   ./scripts/capture-pane.sh ready fresh_launch agp-claude-reviewer
#   ./scripts/capture-pane.sh captures latest --wezterm 123

set -euo pipefail

backend="tmux"
category=""
name=""
session=""
force="false"

for arg in "$@"; do
    case "$arg" in
        --tmux)    backend="tmux" ;;
        --wezterm) backend="wezterm" ;;
        --force)   force="true" ;;
        *)
            if [ -z "$category" ]; then
                category="$arg"
            elif [ -z "$name" ]; then
                name="$arg"
            else
                session="$arg"
            fi
            ;;
    esac
done

[ -n "$category" ] && [ -n "$name" ] || {
    echo "Usage: capture-pane.sh <category> <name> [session] [--tmux|--wezterm] [--force]" >&2
    exit 1
}

case "$category" in
    ready|working|turns|gates|shell|scrollback|edge|captures) ;;
    *)
        echo "Invalid category '$category'" >&2
        exit 1
        ;;
esac

case "$name" in
    */*|.*|*..*|"")
        echo "Invalid capture name '$name'" >&2
        exit 1
        ;;
esac
case "$name" in
    *[!A-Za-z0-9._-]*)
        echo "Invalid capture name '$name'" >&2
        exit 1
        ;;
esac

if [ -z "$session" ]; then
    if [ "$backend" = "wezterm" ]; then
        session="$(wezterm cli list --format json | python3 -c 'import json,sys; panes=json.load(sys.stdin); print(panes[0]["pane_id"] if panes else "")')"
    else
        session="${SESSION:-agp-claude-reviewer}"
    fi
fi

[ -n "$session" ] || { echo "No pane/session provided or discoverable" >&2; exit 1; }

root="$(cd "$(dirname "$0")/.." && pwd)"
dir="$root/smallops_tests/claude_code/corpus/$category"
mkdir -p "$dir"

if [ "$force" != "true" ]; then
    if [ "$backend" = "wezterm" ]; then
        [ ! -e "$dir/${name}.txt" ] || { echo "Capture exists: $dir/${name}.txt (use --force)" >&2; exit 1; }
    else
        [ ! -e "$dir/${name}.raw" ] || { echo "Capture exists: $dir/${name}.raw (use --force)" >&2; exit 1; }
        [ ! -e "$dir/${name}.txt" ] || { echo "Capture exists: $dir/${name}.txt (use --force)" >&2; exit 1; }
    fi
fi

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/smallops-capture.XXXXXX")"
cleanup() {
    find "$tmpdir" -type f -delete 2>/dev/null || true
    rmdir "$tmpdir" 2>/dev/null || true
}
trap cleanup EXIT

publish_file() {
    local src="$1"
    local dst="$2"
    local bak="$tmpdir/$(basename "$dst").bak"
    local had_dst="false"
    if [ -e "$dst" ]; then
        mv "$dst" "$bak"
        had_dst="true"
    fi
    if mv "$src" "$dst"; then
        [ "$had_dst" = "false" ] || rm -f "$bak"
        return 0
    fi
    [ "$had_dst" = "false" ] || mv "$bak" "$dst"
    return 1
}

publish_tmux_pair() {
    local raw_src="$1"
    local txt_src="$2"
    local raw_dst="$3"
    local txt_dst="$4"
    local raw_bak="$tmpdir/$(basename "$raw_dst").bak"
    local txt_bak="$tmpdir/$(basename "$txt_dst").bak"
    local had_raw="false"
    local had_txt="false"

    if [ -e "$raw_dst" ]; then
        mv "$raw_dst" "$raw_bak"
        had_raw="true"
    fi
    if [ -e "$txt_dst" ]; then
        mv "$txt_dst" "$txt_bak"
        had_txt="true"
    fi

    if mv "$raw_src" "$raw_dst" && mv "$txt_src" "$txt_dst"; then
        [ "$had_raw" = "false" ] || rm -f "$raw_bak"
        [ "$had_txt" = "false" ] || rm -f "$txt_bak"
        return 0
    fi

    rm -f "$raw_dst" "$txt_dst"
    [ "$had_raw" = "false" ] || mv "$raw_bak" "$raw_dst"
    [ "$had_txt" = "false" ] || mv "$txt_bak" "$txt_dst"
    return 1
}

if [ "$backend" = "wezterm" ]; then
    wezterm cli get-text --pane-id "$session" > "$tmpdir/${name}.txt"
    publish_file "$tmpdir/${name}.txt" "$dir/${name}.txt"
else
    tmux capture-pane -t "$session" -p -e > "$tmpdir/${name}.raw"
    tmux capture-pane -t "$session" -p    > "$tmpdir/${name}.txt"
    publish_tmux_pair "$tmpdir/${name}.raw" "$tmpdir/${name}.txt" "$dir/${name}.raw" "$dir/${name}.txt"
fi

echo "$dir/${name}.txt"
