# Codex smallops tests

Codex owns its parser and live canaries under this package.

- `test_offline_parser.py` uses synthetic and captured-style screens to assert parser, status, classifier, and gate invariants.
- `test_live.py` runs env-gated local Codex canaries.
- `test_docker.py` runs env-gated Docker canaries against pinned Codex and shared mux binaries.

Live Codex runs use the local `codex` CLI configuration by default. Set
`SMALLOPS_CODEX_LIVE_FLAGS` and/or `SMALLOPS_CODEX_LIVE_MODEL` to select a
provider/model without changing Docker settings, for example:

```sh
SMALLOPS_CODEX_LIVE_FLAGS='--dangerously-bypass-approvals-and-sandbox -c model_provider=openrouter -c model_reasoning_effort=low'
SMALLOPS_CODEX_LIVE_MODEL=openai/gpt-5.6-luna-pro
```

If your local Codex profile files are already migrated, profile flags such as
`-p openrouter` are also fine.

Mux coverage is centralized through the `smallops_mux` pytest parameter in
`smallops_tests/conftest.py` and the mux factory in `smallops_tests/helpers/harness.py`.
The shared mux set currently covers `tmux`, `wezterm`, and `herdr` where the
corresponding binary/server is available.

Useful focused runs:

```sh
SMALLOPS_LIVE=1 python -m pytest smallops_tests/codex/test_live.py -k herdr
SMALLOPS_CODEX_MODEL=openai/gpt-5.6-luna-pro \
SMALLOPS_DOCKER_ENV_FILE=.env.openrouter \
SMALLOPS_DOCKER_PYTEST_ARGS='smallops_tests/codex/test_docker.py -m docker -k herdr -q --tb=short' \
./scripts/docker/run-smallops-tests.sh
```
