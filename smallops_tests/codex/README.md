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
