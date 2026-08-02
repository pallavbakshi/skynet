# Claude Code smallops tests

Claude Code owns its parser corpus and live canaries under this package.

- `test_offline_parser.py` exercises saved corpus captures with invariant-only checks.
- `test_live.py` runs env-gated local Claude Code canaries.
- `test_docker.py` runs env-gated Docker canaries against pinned Claude Code and mux binaries.
- `corpus/` contains input captures only; there are no blessed expected-output snapshots.

Mux coverage is centralized through the `smallops_mux` pytest parameter in
`smallops_tests/conftest.py` and the mux factory in `smallops_tests/helpers/harness.py`.
