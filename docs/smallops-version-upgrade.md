# smallops TUI Version Upgrade Process

Use this process whenever bumping Claude Code, Codex, WezTerm, Herdr, tmux, or a
model/provider setting that can change TUI behavior.

## Goal

Qualify the new version with live Docker workflows, collect real screen captures,
and promote only useful captures into the offline parser corpus. Do not add
blessed expected-output snapshots.

## Preconditions

- Docker is running.
- `.env.openrouter` exists locally and is not committed.
- The Dockerfile pin has been changed intentionally.
- The matching version assertion in `smallops_tests/<tui>/test_docker.py` has
  been updated.

## Qualification Command

```sh
SMALLOPS_DOCKER_ENV_FILE=.env.openrouter make test-smallops-qualify
```

By default this runs all Docker-marked smallops tests across all configured
muxes and writes candidate corpora inside the Docker artifact directory:

- `claude-code-corpus-candidate/`
- `codex-corpus-candidate/`

The script prints the final artifact directory as:

```text
Docker smallops artifacts: test-artifacts/docker/<timestamp>
```

## What The Run Covers

The Docker qualification suite checks:

- pinned binary versions;
- pristine first-run homes;
- scripted bootstrap gates;
- exact reply;
- tool/read usage;
- file write;
- fix failing pytest;
- session API surface: `up`, `down`, `reset`, `send`, `nudge`, `wait`, `peek`,
  `read`, `meta`, `is_alive`, `interrupt`;
- active-turn interrupt;
- muxes: `tmux`, `wezterm`, `herdr`.

## Reviewing Candidate Captures

Inspect the generated candidate corpus before copying anything into
`smallops_tests/<tui>/corpus/`.

Promote captures when they add a new structural screen shape:

- new gate/dialog;
- new status bar or welcome card layout;
- new prompt/composer shape;
- new tool-call/result rendering;
- active working screen;
- error/fatal screen;
- mux-specific wrapping or scrollback behavior.

Do not promote captures merely because timestamps, paths, marker IDs, model
names, or generated prose differ.

## Promotion

Copy selected candidate `*.txt` and `*.raw` files into the matching corpus
category:

- `ready/`
- `working/`
- `turns/`
- `gates/`
- `shell/`
- `scrollback/`
- `edge/`

Use stable names such as `post_response_herdr.txt` or
`tool_read_wezterm.raw`. Keep both raw and normalized text when available.

## Validation After Promotion

Run:

```sh
make test-smallops
make lint
```

For a full local check:

```sh
make test
```

## Failure Triage

If qualification fails, inspect the artifact directory first:

- `docker-run.log`
- `docker-build.log`
- `host-env.txt`
- per-test failure artifacts under `smallops/`
- mux diagnostics under `mux/`
- `claude-version.txt`, `codex-version.txt`, `tmux-version.txt`,
  `wezterm-version.txt`, `herdr-version.txt`

Fix parser/classifier behavior structurally. Prefer prompt glyphs, response
markers, status bars, gate boundaries, and active spinner shape over exact
English verbs or prose.

## Version Bump Checklist

1. Change the Dockerfile pin.
2. Update the corresponding Docker version assertion.
3. Run `SMALLOPS_DOCKER_ENV_FILE=.env.openrouter make test-smallops-qualify`.
4. Review candidate captures.
5. Promote useful captures only.
6. Run `make test-smallops`.
7. Run `make lint`.
8. Commit the version bump, parser fixes, and promoted corpus inputs together.
