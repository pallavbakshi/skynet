# Codex Corpus

Real Codex TUI screen captures collected from the pinned smallops Docker test
image.

- Codex CLI: `@openai/codex@0.146.0`
- WezTerm: `20260117-154428-05343b38`
- Muxes: `tmux`, `wezterm`
- Source run: `test-artifacts/docker/20260802-180753`

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
