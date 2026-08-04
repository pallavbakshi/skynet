# AGP CLI Guide

Practical guide to orchestrating work across AI agents using the `agp` command-line tool.

## Core Concepts

- **Control Plane (CP)** — coordination server at `http://127.0.0.1:7860`. All commands talk to it. Stores all state.
- **Agent** — a registered AI worker (Claude, Codex, etc.) with an ID and capabilities. One agent per runtime.
- **Runtime** — supervisor process that heartbeats to the CP, claims work, drives the agent in a terminal session.
- **Job** — a unit of work: `queued` → `running` → `completed` / `failed` / `cancelled`.
- **Artifact** — output attached to a job. Roles: `result` (clean output), `transcript_log` (full session), `exec_log` (raw log), `prompt` (what was sent).

## Quick Reference

```
agp status                          # system dashboard
agp send <agent> "task"             # send work, wait for result
agp send <agent> "task" --fire-and-forget    # fire and forget
agp wait <job_id> [job_id2 ...]     # wait for one or more jobs
agp result <job_id>                 # get clean output
agp peek <agent>                    # see agent's live terminal
agp info <agent>                    # agent deep-dive
agp jobs                            # list recent jobs
```

## Status and Discovery

```bash
# System dashboard — CP health, runtimes, agents, queue depth
agp status

# Minimal agent list (good for scripts)
agp status -q

# Machine-readable
agp status --json

# Check a specific job or agent
agp status <job_id>
agp status <agent_id>

# Agent deep-dive — capabilities, heartbeat, workspace, current job
agp info <agent_id>

# Add runtime logs and registration details
agp info <agent_id> --diagnose

# Runtime info — bound agents, heartbeat, host
agp info <runtime_id>
```

## Sending Tasks

### Basics

```bash
# Wait for result (auto-detaches after 300s if still running)
agp send <agent_id> "Your task here"

# No quoting needed — words after agent ID are joined
agp send <agent_id> Read src/main.py and find bugs

# Longer wait window
agp send <agent_id> "Complex analysis" --timeout 300

# Fire-and-forget — returns job ID immediately
agp send <agent_id> "Long task" --fire-and-forget
```

### Complex prompts

```bash
# Read task from a file (avoids shell quoting issues)
agp send <agent_id> --via-file /tmp/task.md

# Pipe from stdin
echo "Analyze this" | agp send <agent_id> -
cat report.txt | agp send <agent_id> "Summarize this" -

# Attach files as context
agp send <agent_id> "Review this" --attach config.yaml:context
agp send <agent_id> "Merge these" --attach a.py:input --attach b.py:input
```

### Output contracts

Force structured JSON output:

```bash
agp send <agent_id> "Classify this bug" \
  --output-contract '{"format":"json","json_schema":{"type":"object","required":["severity"],"properties":{"severity":{"type":"string","enum":["high","medium","low"]}}}}'
```

## Collecting Results

### Wait for jobs

```bash
# Single job — blocks until done, prints result inline
agp wait <job_id>

# Multiple jobs — streams results as each completes
agp wait <job_a> <job_b> <job_c>

# With timeout
agp wait <job_id> --poll-timeout 600
```

`wait` prints the result inline — you don't need a separate `result` call unless you want to re-fetch later.

### Fetch result separately

```bash
# Default: prefers transcript_log > result > exec_log
agp result <job_id>

# Specific artifact role
agp result <job_id> --role result
agp result <job_id> --role transcript_log
```

### List recent jobs

```bash
agp jobs
agp jobs --limit 20
agp jobs --agent <agent_id>
agp jobs --status failed
```

## Parallel Dispatch

Send to multiple agents, then wait for all:

```bash
# Dispatch (note job IDs from output)
agp send reviewer-a "Review for correctness" --fire-and-forget   # → job_aaa
agp send reviewer-b "Review for security" --fire-and-forget      # → job_bbb

# Wait for both — results stream as each finishes
agp wait job_aaa job_bbb --poll-timeout 300
```

The faster reviewer's result appears immediately; you don't block on the slower one.

## Live Inspection

### Peek — terminal snapshot

```bash
# See what the agent is doing right now
agp peek <agent_id>

# Include scrollback history
agp peek <agent_id> --lines 200
```

Works for local agents (instant via tmux) and remote agents (via CP heartbeat, ~5-15s).

### Attach — interactive terminal

```bash
agp attach <agent_id>
```

Opens the agent's tmux session directly. Local agents only. Use `peek` for remote agents.

### Nudge — inject text into terminal

```bash
agp nudge <agent_id> "Stop what you're doing and prioritize the auth fix"
```

Injects text directly into the agent's terminal session via the next heartbeat.

## Multi-Turn Conversations

Reply to a completed job — the agent sees its previous work:

```bash
agp reply <job_id> "Now fix what you found"
agp reply <job_id> "Follow up on the security issue" --fire-and-forget
```

Supports the same flags as `send` (`--attach`, `--timeout`, `--via-file`, etc.).

## Review Loops

### Automated review (recommended)

```bash
# Reviewer checks output of a dev job
agp review <source_job_id> <reviewer_agent_id>

# With a specific dev agent for fix cycles
agp review <source_job_id> <reviewer_agent_id> --dev <dev_agent_id>

# Attach local git diff as context
agp review <source_job_id> <reviewer_agent_id> --diff

# Max rounds (default: 3)
agp review <source_job_id> <reviewer_agent_id> --max-rounds 5
```

The review loop: reviewer returns `{"verdict": "approved"|"changes_requested", "findings": [...]}`. If changes requested, findings go to the dev agent, dev fixes, reviewer re-reviews. Repeats until approved or max rounds.

### Manual dual review

When you want two reviewers in parallel, use `--review` for structured output:

```bash
agp send reviewer-a "Review this change" --review --fire-and-forget
agp send reviewer-b "Review this change" --review --fire-and-forget
agp wait <job_a> <job_b>
```

### Review session management

```bash
agp review-status <source_job_id>       # check progress
agp review-diagnose <source_job_id>     # diagnose stuck session
agp review --resume <source_job_id>     # resume interrupted review
```

## Agent Lifecycle

```bash
# Provision from a registered capability
agp up <capability_name>
agp up <capability_name> --agent-id my-custom-name
agp up <capability_name> --workspace /path/to/workdir

# Tear down
agp down <agent_id>
agp down <agent_id> --force    # cancels active jobs

# Interrupt current work (agent stays alive)
agp interrupt <agent_id>
agp interrupt <agent_id> --purge   # also clear the queue
agp interrupt <job_id>             # cancel a specific job
```

## Diagnosing Stalls

When a job appears stuck:

```bash
# 1. Check job status and activity
agp status <job_id>

# 2. Look at the agent's terminal
agp peek <agent_id>
agp peek <agent_id> --lines 50

# 3. Agent diagnostics — runtime binding, logs, job history
agp info <agent_id> --diagnose
```

**Common causes** — most stalls are infrastructure, not the agent:
- **Permission gates** — auto-dismissed but each adds ~5-10s
- **Context compaction** — long sessions trigger compaction, pausing output for several seconds
- **Network latency** — model may take 30-60s on complex queries. If heartbeats are fresh, the agent is alive

## Workflow Recipes

### Dev + dual review pipeline

```bash
# 1. Dev implements
agp send dev-agent "Implement rate limiting on /api/submit" --fire-and-forget
agp wait job_impl --poll-timeout 600

# 2. Two reviewers in parallel
agp send reviewer-a "Review for correctness" --review --fire-and-forget
agp send reviewer-b "Review for security" --review --fire-and-forget
agp wait job_rev_a job_rev_b --poll-timeout 300

# 3. Dev fixes based on review findings
agp reply job_impl "Fix these issues: <paste findings>" --fire-and-forget
```

### Chain agent outputs

```bash
# Agent A analyzes, agent B acts on it
agp send analyst "Find the top 5 error patterns" --timeout 120
agp result job_analysis | agp send dev-agent -
```

### Iterative fix + review

```bash
agp send dev-agent "Fix the memory leak in worker.py" --timeout 300
agp review job_fix reviewer-agent --dev dev-agent --diff --max-rounds 3
```

## Tips

- **`wait` accepts multiple job IDs** — `agp wait job_a job_b job_c` streams results as each completes. No need to wait sequentially.
- **`--fire-and-forget` for parallel work** — send multiple tasks with `--fire-and-forget`, then collect with one `agp wait`.
- **`--via-file` for complex prompts** — avoids shell quoting nightmares. Write your prompt in a file, pass the path.
- **`peek` works remotely** — as long as CLI and runtime talk to the same CP. Timeout is 45s to accommodate heartbeat cycles over tunnels.
- **`status` is the dashboard** — no args for system overview, pass a job/agent ID for details.
- **`result` is for re-fetching** — `wait` already prints results inline when jobs complete.

## Housekeeping

```bash
agp cleanup    # remove temp artifacts and stale result files
```
