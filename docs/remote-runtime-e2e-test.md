# Remote Runtime E2E Test

This runbook covers the topology we actually use:

- control plane on Ubuntu server `user` at `your-server.example.com`
- runtime on the Mac
- Codex session on the Mac via `tmux` or `WezTerm`
- artifacts uploaded back to the Ubuntu control plane over HTTP

The default path below uses `tmux` and `make runtime-remote`.

## Topology

```text
┌─────────────────────────┐                  ┌─────────────────────────┐
│ Ubuntu server (user)      │                  │ Mac                     │
│ your-server.example.com            │                  │                         │
│                         │  claim / hb /    │ agp runtime-work-loop   │
│ agp serve               │◄────────────────►│ tmux or WezTerm         │
│ agp sweep-loop          │   artifact HTTP  │ ncodex / codex          │
│ agp sweep-runtimes-loop │                  │                         │
│ .agp-artifacts/         │                  │ local checkout          │
└─────────────────────────┘                  └─────────────────────────┘
```

## Assumptions

### Ubuntu (`user`)

- repo at `~/projects/skynet`
- virtualenv exists at `.venv`
- firewall allows `7860/tcp`
- `make local-serve` works

### Mac

- repo at `~/projects/skynet`
- virtualenv exists at `.venv`
- `tmux` or `wezterm` installed
- at least one provider key is set:
  - `OPENAI_API_KEY`, or
  - `OPENROUTER_API_KEY`
- your manual Codex command works before you involve AGP

Recommended manual check on the Mac:

```bash
ncodex -a never -s danger-full-access "What is 2 + 2? Reply with just the number."
```

If your working manual command is different, override
`AGP_CODEX_CLI_COMMAND` when starting the runtime.

## One-Time Setup

On Ubuntu, create `skyops.local.toml` so the seeded agent points at the Mac
workspace path instead of a Linux path:

```toml
[agents.agt_local]
workspace_ref = "/Users/<mac-user>/projects/skynet"
```

Do not commit that file.

## 1. Start The Control Plane On Ubuntu

On Ubuntu:

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

```bash
make stop
```

```bash
make local-reset
```

```bash
make local-initdb
```

If `ufw` is enabled:

```bash
sudo ufw allow 7860/tcp
```

Start the control plane and sweepers:

```bash
make local-serve
```

Seed after the control plane is healthy:

```bash
make local-seed
```

In a second Ubuntu shell, verify health:

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

```bash
curl -s http://127.0.0.1:7860/health | python3 -m json.tool
```

```bash
curl -s http://your-server.example.com:7860/health | python3 -m json.tool
```

Expected result:

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "components": {
      "api": "ok",
      "db": "ok"
    }
  }
}
```

## 2. Verify Reachability From The Mac

On the Mac:

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

```bash
curl -s http://your-server.example.com:7860/health | python3 -m json.tool
```

Do not start the runtime until this works.

## 3. Start The Runtime On The Mac

### Default tmux path

On the Mac:

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

Clean up any prior runtime worker or terminal session:

```bash
make runtime-clean
```

Start the remote runtime:

```bash
make runtime-remote
```

Current defaults from the `Makefile`:

- `AGP_REMOTE_SERVER_URL=http://your-server.example.com:7860`
- `AGP_RUNTIME_ID=rtm_mac`
- `AGP_RUNTIME_AGENT_ID=agt_local`
- `AGP_CODEX_CLI_COMMAND="ncodex -a never -s danger-full-access"`

If your working local Codex command is different:

```bash
AGP_CODEX_CLI_COMMAND="codex -a never -s danger-full-access" make runtime-remote
```

### WezTerm alternative

If you want WezTerm instead of tmux:

```bash
make runtime-wezterm
```

## 4. Send A Smoke Job

From Ubuntu:

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

```bash
python -m agp send agt_local "What is 2 + 2? Reply with just the number." --server-url http://your-server.example.com:7860
```

That prints a `JOB_ID`.

Wait for completion:

```bash
python -m agp wait <JOB_ID> --server-url http://your-server.example.com:7860
```

List jobs if needed:

```bash
python -m agp jobs --server-url http://your-server.example.com:7860
```

## 5. Verify The Result

### Control-plane view on Ubuntu

```bash
python -m agp status <JOB_ID> --server-url http://your-server.example.com:7860
```

For the default runtime id `rtm_mac`, the result artifact is stored on Ubuntu at:

```bash
cat .agp-artifacts/rtm_mac/<JOB_ID>/result.txt
```

Expected output for the smoke prompt:

```text
4
```

### Terminal view on the Mac

If you used `tmux`:

```bash
tmux capture-pane -t agp-agt_local -p | tail -40
```

If you used WezTerm:

```bash
wezterm cli list --format json
```

## 6. Cleanup

### Mac

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

```bash
make runtime-clean
```

### Ubuntu

```bash
cd ~/projects/skynet
```

```bash
source .venv/bin/activate
```

```bash
make stop
```

## Optional: SSH Tunnel Instead Of Opening Port 7860

If you do not want to expose `7860/tcp` publicly, use an SSH tunnel from the
Mac:

```bash
ssh -N -L 7860:127.0.0.1:7860 user@your-server.example.com
```

Then start the runtime against the tunneled address:

```bash
AGP_REMOTE_SERVER_URL=http://127.0.0.1:7860 make runtime-remote
```

In that mode, send and wait commands should also use:

```bash
--server-url http://127.0.0.1:7860
```

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| `curl http://your-server.example.com:7860/health` times out from the Mac | Ubuntu ingress blocked | `sudo ufw allow 7860/tcp`, then retest |
| `make runtime-remote` sits there with no output | Normal idle worker loop | Send a job; the runtime is long-lived |
| Job is accepted but never finishes | Codex CLI path on the Mac is wrong | Run your manual `ncodex` or `codex` command first, then set `AGP_CODEX_CLI_COMMAND` to match it |
| Runtime starts but claims nothing | Wrong agent id | Keep `AGP_RUNTIME_AGENT_ID=agt_local` or reseed a matching agent |
| Codex opens in the wrong directory | Wrong `workspace_ref` on Ubuntu seed config | Fix `skyops.local.toml`, then `make local-reset && make local-initdb && make local-seed` |
| No result artifact on Ubuntu | Runtime not using HTTP artifact backend | `make runtime-remote` and `make runtime-wezterm` already set `AGP_ARTIFACT_BACKEND=http` |
| `tmux capture-pane` shows provider env vars | tmux transcript contains launch env | Treat tmux transcript artifacts as sensitive |

## Known Good Path

This is the shortest happy path after one-time setup:

### Ubuntu

```bash
source .venv/bin/activate
make stop
make local-reset
make local-initdb
sudo ufw allow 7860/tcp
make local-serve
make local-seed
```

### Mac

```bash
source .venv/bin/activate
make runtime-clean
make runtime-remote
```

### Ubuntu

```bash
source .venv/bin/activate
python -m agp send agt_local "What is 2 + 2? Reply with just the number." --server-url http://your-server.example.com:7860
python -m agp wait <JOB_ID> --server-url http://your-server.example.com:7860
cat .agp-artifacts/rtm_mac/<JOB_ID>/result.txt
```
