# Claude Code TUI Corpus

Real tmux pane captures of Claude Code at various TUI states.
Used as ground truth for the parser test suite.

## How to capture

```bash
# One command — captures raw, plain, scrollback, and metadata:
./scripts/capture-pane.sh <category> <name> [session]

# Or via make:
make capture CAT=ready NAME=fresh_launch
make capture CAT=working NAME=thinking SESSION=agp-claude-reviewer
```

## What gets saved

Each capture produces 4 files:

| File | Content |
|------|---------|
| `{name}.raw` | Raw tmux output with ANSI escapes (source of truth) |
| `{name}.txt` | Plain text (ANSI stripped by tmux) |
| `{name}.scrollback.txt` | Full scrollback history (plain) |
| `{name}.capture.json` | Metadata: timestamp, pane size, cursor position, version |

Optional: `{name}.expected.json` — ground-truth assertions for tests.

## Directory layout

- `ready/` — Idle prompt states (fresh launch, post-response)
- `working/` — Active processing (thinking, tool use)
- `turns/` — Completed conversations (single, multi-turn, tool results)
- `gates/` — Permission/trust/login screens
- `shell/` — Post-exit shell prompt states
- `scrollback/` — Full read_output accumulations
- `edge/` — Edge cases and regression captures

## When to capture

Capture whenever you see a new TUI state:
- New gate/dialog type
- Different spinner verb or frame
- Unusual layout (compaction, error box, swarm mode)
- After Claude Code version updates

Each new capture is a regression guard for the parser.
