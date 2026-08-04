# Codex Corpus

Real Codex TUI screen captures collected from the pinned smallops Docker test
image.

- Codex CLI: `@openai/codex@0.146.0`
- WezTerm: `20260117-154428-05343b38`
- Muxes: `tmux`, `wezterm`, `herdr`
- Source run: `test-artifacts/docker/20260804-025714`

This corpus intentionally mixes two capture generations:

- **Current (via-file delivery):** `exact_reply`, `api_send`, `active_turn`,
  and `post_response` across `tmux`, `wezterm`, and `herdr`. Prompts appear as
  `› Read the file /tmp/smallops/task-…` — the post-via-file Codex delivery
  shape, captured from the pinned Docker qualification flow.
- **Legacy (inline-marker delivery):** `after_reset`, `file_write`, `test_fix`,
  and `tool_read` across `tmux` and `wezterm`. Prompts appear as
  `› SMALLOPS-CODEX-TASK-…` — the pre-via-file shape, retained for parser
  backwards-compat (the marker-agnostic parser must handle both).

These files are not blessed expected outputs. Offline tests assert parser and
classifier invariants only: parsing must not throw, status fields must stay
well-typed, ready/turn screens classify as `READY`, and working captures remain
valid `IdleReason` values.

To refresh the corpus, run the Codex Docker suite with:

```sh
SMALLOPS_CODEX_CORPUS_OUT=/app/test-artifacts/docker-current/codex-corpus \
SMALLOPS_DOCKER_PYTEST_ARGS='smallops_tests/codex/test_docker.py -q --tb=short' \
scripts/docker/run-smallops-tests.sh
```

The preferred version-bump path is:

```sh
SMALLOPS_DOCKER_ENV_FILE=.env.openrouter make test-smallops-qualify
```

Review the generated `codex-corpus-candidate/` directory under the Docker
artifact run before promoting useful captures into this corpus.
