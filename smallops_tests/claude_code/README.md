# Claude Code smallops tests

Claude Code owns its parser corpus and live canaries under this package.

- `test_offline_parser.py` exercises saved corpus captures with invariant-only checks.
- `test_live.py` runs env-gated local Claude Code canaries.
- `test_docker.py` runs env-gated Docker canaries against pinned Claude Code and mux binaries.
- `corpus/` contains input captures only; there are no blessed expected-output snapshots.

Mux coverage is centralized through the `smallops_mux` pytest parameter in
`smallops_tests/conftest.py` and the mux factory in `smallops_tests/helpers/harness.py`.
The shared mux set currently covers `tmux`, `wezterm`, and `herdr` where the
corresponding binary/server is available.

Useful focused runs:

```sh
SMALLOPS_LIVE=1 python -m pytest smallops_tests/claude_code/test_live.py -k herdr
SMALLOPS_DOCKER_ENV_FILE=.env.openrouter \
SMALLOPS_DOCKER_PYTEST_ARGS='smallops_tests/claude_code/test_docker.py -m docker -k herdr -q --tb=short' \
./scripts/docker/run-smallops-tests.sh
make capture CAT=captures NAME=claude_herdr_latest MUX=herdr SESSION=w1:p1
```
