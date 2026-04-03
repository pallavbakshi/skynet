# Claude Code TUI Corpus

Real tmux pane captures of Claude Code at various TUI states.
Used as ground truth for the parser test suite.

## How to capture

```bash
# Visible screen only (most common):
tmux capture-pane -t <pane> -p > corpus/<category>/<name>.txt

# With ANSI escapes (for strip_ansi robustness testing):
tmux capture-pane -t <pane> -p -e > corpus/<category>/<name>.ansi.txt

# Full scrollback:
tmux capture-pane -t <pane> -p -S - > corpus/<category>/<name>.txt
```

## Directory layout

- `ready/` — Idle prompt states (fresh launch, post-response, with welcome box)
- `working/` — Active processing (thinking, tool use in progress)
- `turns/` — Completed conversations (single turn, multi-turn, with tool results)
- `gates/` — Gate/permission/trust/login screens
- `shell/` — Post-exit shell prompt states
- `scrollback/` — Full read_output accumulations
- `edge/` — Edge cases and regression captures

## Sidecar files

Each `.txt` can have an optional `.expected.json` sidecar with ground-truth assertions:

```json
{
  "classify": {
    "is_ready": true,
    "is_working": false,
    "ends_with_prompt": true,
    "is_shell_returned": false,
    "gate_kind": "NONE"
  },
  "turns": [
    {"prompt": "hello", "response_starts_with": "Hello"}
  ],
  "last_response_contains": "Hello"
}
```

## When to update

Update captures when Claude Code ships UI changes that affect:
- Prompt markers (❯)
- Response markers (⏺, ●)
- Status bar format (⏵⏵)
- Gate/permission dialogs
- Welcome box layout
- Thinking/working indicators
