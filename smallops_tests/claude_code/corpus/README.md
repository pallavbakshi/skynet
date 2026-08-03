# Claude Code TUI Corpus

Real pane captures of Claude Code at various TUI states.
Used as offline inputs for smallops parser property tests. These are not blessed
snapshots; tests assert invariants and category-level properties only.

## How to capture

```bash
# One command — captures raw and plain screen text:
./scripts/capture-pane.sh <category> <name> [session]

# Or via make:
make capture CAT=ready NAME=fresh_launch
make capture CAT=working NAME=thinking SESSION=agp-claude-reviewer
make capture CAT=ready NAME=fresh_launch FORCE=1  # overwrite intentionally
```

## What gets saved

Each capture should produce 2 files:

| File | Content |
|------|---------|
| `{name}.raw` | Raw tmux output with ANSI escapes (source of truth) |
| `{name}.txt` | Plain text (ANSI stripped by tmux) |

Do not add `{name}.expected.json` files. The offline layer intentionally avoids
snapshot equality.

Docker version qualification can also emit candidate captures:

```bash
SMALLOPS_DOCKER_ENV_FILE=.env.openrouter make test-smallops-qualify
```

Review the generated `claude-code-corpus-candidate/` directory under the Docker
artifact run before promoting useful captures into this corpus.

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
