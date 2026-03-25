#!/usr/bin/env bash
set -euo pipefail
# E2E test: Claude Code TUI on WezTerm mux-server inside Docker.

export HOME=/home/agpuser
export PATH=/usr/local/bin:/usr/bin:/bin
export DISABLE_AUTOUPDATER=1
export DISABLE_TELEMETRY=1
export WEZTERM_CONFIG_FILE=/etc/wezterm/wezterm.lua

# Complete first-run for -p mode (writes settings etc.)
echo "[setup] Completing Claude Code first-run..."
claude --dangerously-skip-permissions -p "test" >/dev/null 2>&1 || true

echo "[setup] Starting wezterm-mux-server..."
wezterm-mux-server --daemonize 2>/dev/null
sleep 2

PANE_ID=$(wezterm cli spawn --new-window)
echo "[setup] Pane: $PANE_ID"
wezterm cli list --format json | python3 -c "
import json,sys
for p in json.load(sys.stdin):
    print(f\"  pane {p['pane_id']}: {p.get('cols','?')}x{p.get('rows','?')}\")
"

echo "[test] Launching Claude Code..."
wezterm cli send-text --pane-id "$PANE_ID" --no-paste "claude --dangerously-skip-permissions"
sleep 0.05
wezterm cli send-text --pane-id "$PANE_ID" --no-paste $'\r'

for i in $(seq 1 16); do
    sleep 5
    SCREEN=$(wezterm cli get-text --pane-id "$PANE_ID" --start-line -50 2>&1)
    LOWER=$(echo "$SCREEN" | tr '[:upper:]' '[:lower:]')

    # Handle gate screens (patterns must NOT match the status bar)
    if echo "$LOWER" | grep -q 'bypass permissions mode'; then
        echo "[$i] Gate: bypass permissions -> 2"
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste "2"
        sleep 0.05
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste $'\r'
        continue
    fi
    if echo "$LOWER" | grep -q 'quick safety check\|yes, i trust this folder'; then
        echo "[$i] Gate: trust -> 1"
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste "1"
        sleep 0.05
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste $'\r'
        continue
    fi
    if echo "$LOWER" | grep -q 'choose the text style'; then
        echo "[$i] Gate: theme -> Enter"
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste $'\r'
        continue
    fi
    if echo "$LOWER" | grep -q 'login successful\|security notes'; then
        echo "[$i] Gate: continue -> Enter"
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste $'\r'
        continue
    fi
    if echo "$LOWER" | grep -q 'select login method\|paste code here'; then
        echo "[$i] FATAL: OAuth login required"
        exit 1
    fi

    # Check for ready prompt: look for the prompt character
    HAS_PROMPT=$(echo "$SCREEN" | grep -c $'\xe2\x9d\xaf' || true)  # ❯ in UTF-8
    HAS_SEPARATOR=$(echo "$SCREEN" | grep -cE $'\xe2\x94\x80{4,}' || true)  # ────
    HAS_RESPONSE=$(echo "$SCREEN" | grep -c $'\xe2\x8f\xba' || true)  # ⏺
    HAS_BOX=$(echo "$SCREEN" | grep -c $'\xe2\x95\xad' || true)  # ╭

    if [ "$HAS_PROMPT" -gt 0 ] && [ $((HAS_SEPARATOR + HAS_RESPONSE + HAS_BOX)) -gt 0 ]; then
        echo "[$i] Claude Code TUI ready!"
        echo "$SCREEN" | tail -5

        echo ""
        echo "[test] Sending: What is 6 * 7? Reply with just the number."
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste "What is 6 * 7? Reply with just the number."
        sleep 0.15
        wezterm cli send-text --pane-id "$PANE_ID" --no-paste $'\r'

        echo "[test] Waiting for response..."
        PREV=""
        UNCHANGED=0
        for j in $(seq 1 20); do
            sleep 3
            RESP=$(wezterm cli get-text --pane-id "$PANE_ID" --start-line -50 2>&1)
            if [ "$RESP" = "$PREV" ]; then
                UNCHANGED=$((UNCHANGED + 1))
                [ $UNCHANGED -ge 3 ] && break
            else
                UNCHANGED=0
            fi
            PREV="$RESP"
        done

        echo "=== Final screen ==="
        wezterm cli get-text --pane-id "$PANE_ID" --start-line -30 2>&1

        if echo "$RESP" | grep -q "42"; then
            echo ""
            echo "[PASS] Correct answer '42' found!"
            exit 0
        else
            echo ""
            echo "[FAIL] Expected '42' in response"
            exit 1
        fi
    fi

    echo "[$i] Waiting... (last 3 lines):"
    echo "$SCREEN" | grep -v '^$' | tail -3
done

echo "[FAIL] TUI did not become ready after 80s"
wezterm cli get-text --pane-id "$PANE_ID" --start-line -50 2>&1
exit 1
