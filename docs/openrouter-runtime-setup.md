# OpenRouter Runtime Setup Guide

How to configure a machine to run AGP runtimes with Codex CLI through OpenRouter.

## Prerequisites

- AGP installed (`uv pip install -e .` from the skynet repo)
- `codex` or `codex` CLI installed (`npm install -g @openai/codex`)
- tmux or WezTerm installed
- An OpenRouter API key from https://openrouter.ai/keys

## 1. Install / update Codex CLI

The runtime needs a **current** Codex CLI build. Version `0.0.0` (dev build) has
known auth failures against OpenRouter — WebSocket 404 followed by HTTPS 401.

```bash
# Install the latest release
npm install -g @openai/codex

# Verify version (must be >= 0.116.0)
codex --version
```

If you use `codex` (a fork), make sure it is also up to date.

## 2. Set the OpenRouter API key

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
```

Add this to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.) so it persists.

## 3. Configure Codex config.toml

This is the **critical step** that the reviewer's machine was missing. Codex CLI
does not read `OPENROUTER_API_KEY` by default — it needs a `config.toml` that
declares OpenRouter as a model provider.

Create or edit `~/.config/codex/config.toml`:

```toml
# Model to use (OpenRouter model path)
model = "openai/gpt-5.3-codex"

# Tell Codex to route through OpenRouter
model_provider = "openrouter"

# Reasoning effort: "low", "medium", "high"
model_reasoning_effort = "high"

# Provider definition
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"
```

### What each field does

| Field | Purpose |
|-------|---------|
| `model` | The model ID as listed on OpenRouter (e.g. `openai/gpt-5.3-codex`, `anthropic/claude-sonnet-4`) |
| `model_provider` | Which `[model_providers.*]` block to use |
| `base_url` | OpenRouter's API endpoint |
| `env_key` | Which env var holds the API key — Codex reads this at startup |
| `wire_api` | Protocol: `"responses"` for OpenAI-compatible, `"chat"` for older endpoints |

### Why OPENAI_BASE_URL alone is not enough

Older Codex versions accepted `OPENAI_BASE_URL` to redirect requests. Current
versions (0.116+) use the `config.toml` provider system instead. Setting only
the env var without the config.toml results in:

1. WebSocket connection attempt to OpenAI's realtime endpoint → 404
2. Fallback to HTTPS → 401 "Missing Authentication header" (because Codex sends
   the key from `OPENAI_API_KEY`, which is empty)

## 4. Trust the project directory

Codex will prompt for trust on first run in a new directory. Pre-trust the
skynet repo to avoid interactive prompts that block the AGP runtime:

```toml
# Add to ~/.config/codex/config.toml
[projects."/path/to/skynet"]
trust_level = "trusted"
```

Or run Codex manually once in the directory and accept the trust prompt.

## 5. Optional: suppress noisy prompts

Add these to `config.toml` to prevent rate-limit and warning dialogs from
blocking the AGP adapter's polling:

```toml
[notice]
hide_full_access_warning = true
hide_rate_limit_model_nudge = true
```

## 6. Verify the setup

Test Codex standalone before running through AGP:

```bash
# Quick non-interactive test
codex exec "What is the capital of France?"

# Or interactive
codex "What is 2+2?"
```

You should see a response without auth errors. If you see WebSocket 404 or
401 Unauthorized, re-check steps 1-3.

## 7. Run the AGP runtime

### tmux + codex (local CP)

```bash
make runtime
```

### tmux + codex (remote CP)

```bash
export AGP_REMOTE_SERVER_URL=http://<cp-host>:7860
make runtime-remote
```

### WezTerm + codex (remote CP)

```bash
export AGP_REMOTE_SERVER_URL=http://<cp-host>:7860
make runtime-wezterm
```

All three targets set `OPENROUTER_API_KEY` and `OPENAI_BASE_URL` in the
environment. The Codex config.toml is what actually makes Codex use them
correctly.

## Troubleshooting

### "WebSocket 404 / HTTPS 401 Unauthorized"

- **Cause**: Codex CLI doesn't have OpenRouter configured as a provider
- **Fix**: Add the `[model_providers.openrouter]` block to config.toml (step 3)

### "codex version 0.0.0"

- **Cause**: Dev build that predates the provider config system
- **Fix**: Install the latest release: `npm install -g @openai/codex`

### Codex hangs at a trust/permission prompt

- **Cause**: Project directory not trusted, or `-a never` flag not passed
- **Fix**: Add project to `[projects]` in config.toml, or ensure the runtime
  passes `-a never -s danger-full-access`

### Secrets visible in tmux pane transcript

The current codex adapter injects `OPENROUTER_API_KEY=sk-or-...` inline in
the shell command sent to tmux. This means the key appears in:

- tmux scrollback (`tmux capture-pane`)
- The `transcript_log` artifact uploaded to the control plane

**Mitigation**: The tmux host plugin sets env vars via `tmux set-environment`
on session creation, so Codex can read them from the tmux environment. The
inline prefix in `codex.py:_runtime_env_prefix()` is a fallback for cases
where `set-environment` doesn't propagate (e.g. nested shells). If your
setup works without the inline prefix, you can set
`AGP_CODEX_CLI_COMMAND="codex -m MODEL"` without the env prefix.

Long-term fix: move all secrets to tmux session environment only and strip
them from transcript artifacts before upload.

### WezTerm pane spawns on remote SSH host

If your WezTerm GUI is connected to a remote multiplexer (`wezterm connect`),
new panes spawn remotely by default. Set `AGP_WEZTERM_DOMAIN=local` to force
local pane creation:

```bash
export AGP_WEZTERM_DOMAIN=local
```

## Reference: env vars used by the runtime

| Variable | Purpose | Required |
|----------|---------|----------|
| `OPENROUTER_API_KEY` | OpenRouter API key | Yes (for OpenRouter) |
| `OPENAI_BASE_URL` | Override API endpoint | Set automatically by AGP |
| `OPENAI_API_KEY` | Direct OpenAI key | Only if not using OpenRouter |
| `AGP_SERVER_URL` | Control plane URL | Set by Makefile targets |
| `AGP_CODEX_CLI_COMMAND` | Full codex CLI invocation | Set by Makefile targets |
| `AGP_CODEX_TUI_MODE` | Use interactive TUI mode | `true` for tmux/wezterm |
| `AGP_RUNTIME_TERMINAL_HOST_KIND` | `tmux` / `wezterm` / `inprocess` | Set by Makefile targets |
| `AGP_WEZTERM_DOMAIN` | WezTerm domain for pane creation | Optional (`local`) |
