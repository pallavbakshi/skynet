# AGP CLI Guide for Agents

You have access to the `agp` command-line tool for orchestrating work across a fleet of AI agents. This guide covers everything you need to delegate tasks, collect results, run review loops, and manage agent lifecycle.

## Core Concepts

- **Agent**: A running AI instance (Claude, Codex, etc.) registered with the control plane. Each has an ID (e.g. `claude-dev`, `codex-reviewer`) and one or more capabilities (e.g. `code`, `review`).
- **Job**: A unit of work sent to an agent. Has a lifecycle: `queued` -> `running` -> `completed`/`failed`/`cancelled`.
- **Artifact**: Output attached to a job. Common roles: `result` (clean output), `transcript_log` (full session transcript), `exec_log` (raw execution log), `prompt` (what was sent).
- **Control Plane (CP)**: The coordination server at `http://127.0.0.1:7860` (default). All `agp` commands talk to it.

## Discovery: What's Available?

Before sending work, check what agents are running and healthy.

```bash
# Quick health check: CP, runtimes, agents
agp health

# List all agents with status, capabilities, and queue depth
agp ls

# Deep-dive into a specific agent: config, recent jobs, heartbeat
agp info <agent_id>
```

**Pick the right agent for the task.** Match agent capabilities to your need:
- Agents with `code` or `python` capabilities do implementation work.
- Agents with `review` capability do code review and analysis.

## Sending Tasks

### Basic send (synchronous wait)

```bash
agp send <agent_id> "Your task description here"
```

This waits up to 90 seconds for the result. If the agent finishes in time, you get the output inline. If not, it auto-detaches and gives you a job ID.

### Unquoted multi-word tasks

You don't need to quote the task. Extra words after the agent ID are joined:

```bash
agp send <agent_id> Read the file src/main.py and find bugs
```

If the task text contains `send`/`reply` option-looking tokens, unknown ones
are treated as task text, but known flags for that command still need `--` to
terminate option parsing. Near-miss typos of real flags (for example
`--detatch`) are rejected so they don't get silently swallowed into the task:

```bash
agp send <agent_id> -- Explain how --detach differs from auto-detach
```

### Fire-and-forget

```bash
agp send <agent_id> "Long running task" --detach
```

Returns immediately with a job ID. The agent keeps working.

### Longer wait window

```bash
agp send <agent_id> "Complex analysis" --timeout 300
```

Waits up to 300 seconds before auto-detaching.

### Piping from stdin

```bash
echo "Analyze this code" | agp send <agent_id> -
cat requirements.txt | agp send <agent_id> "Check these deps for vulnerabilities" -
```

The `-` reads from stdin. You can also omit the task argument entirely to read from stdin.

### Attaching files

```bash
agp send <agent_id> "Review this config" --attach config.yaml:context
agp send <agent_id> "Merge these" --attach file1.py:input --attach file2.py:input
```

Format: `<path>:<role>`. Role is freeform (e.g. `context`, `input`, `reference`).

### Structured output contracts

Force the agent to respond with a specific JSON schema:

```bash
agp send <agent_id> "Classify this bug" \
  --output-contract '{"format":"json","json_schema":{"type":"object","required":["severity","category"],"properties":{"severity":{"type":"string","enum":["high","medium","low"]},"category":{"type":"string"}}}}'
```

### Execution timeout hint

Tell the runtime to cap execution time:

```bash
agp send <agent_id> "Quick check" --timeout-seconds 60
```

This is separate from `--timeout` (which controls how long the CLI waits). `--timeout-seconds` tells the agent runtime to stop after that many seconds.

## Collecting Results

### Re-attach to a running job

```bash
agp wait <job_id>
agp wait <job_id> --timeout 600
```

### Get clean output

```bash
# Default: prefers transcript_log > result > exec_log
# For jobs with output contracts (e.g. --review): prefers result > transcript_log
agp result <job_id>

# Specific artifact role
agp result <job_id> --role result
agp result <job_id> --role transcript_log
agp result <job_id> --role exec_log
```

### Check job status

```bash
agp status <job_id>
```

### List recent jobs

```bash
agp jobs
agp jobs --limit 20
agp jobs --agent <agent_id>
agp jobs --status completed
agp jobs --agent <agent_id> --status failed
```

## Multi-Turn Conversations (Reply)

Continue a conversation with an agent by replying to a completed job:

```bash
agp reply <job_id> "Follow up on your previous analysis"
agp reply <job_id> "Now fix what you found" --detach
```

Reply preserves conversation context: the agent sees the original prompt and its previous response. Supports the same flags as `send`: `--attach`, `--timeout`, `--timeout-seconds`, `--output-contract`, `--detach`.

## Parallel Dispatch Pattern

Send work to multiple agents simultaneously:

```bash
# Fire-and-forget to two reviewers
agp send reviewer-a "Review src/api.py for security issues" --detach
# note the job ID from output, e.g. job_aaa
agp send reviewer-b "Review src/api.py for performance issues" --detach
# note the job ID from output, e.g. job_bbb

# Wait for both
agp wait job_aaa --timeout 300
agp wait job_bbb --timeout 300

# Or just check results when ready
agp result job_aaa
agp result job_bbb
```

## Automated Review Loops

**Prefer `agp review` over manual `agp send` for review tasks.** The built-in review loop enforces a structured JSON output contract, so reviewers return machine-readable findings instead of transcript-heavy prose. Manual `agp send` to a reviewer returns free-form text that you have to parse yourself.

If you must use manual sends (e.g. to run two reviewers in parallel), add `--review` to get the same structured output:

```bash
agp send reviewer-a "Review this change" --review --detach
agp send reviewer-b "Review this change" --review --detach
```

### Built-in review loop (recommended)

```bash
# Basic review: reviewer checks the output of a dev job
agp review <source_job_id> <reviewer_agent_id>

# Specify the dev agent for fix cycles (defaults to the source job's agent)
agp review <source_job_id> <reviewer_agent_id> --dev <dev_agent_id>

# Attach local git diff as supplementary context
agp review <source_job_id> <reviewer_agent_id> --diff

# Control max rounds (default: 3)
agp review <source_job_id> <reviewer_agent_id> --max-rounds 5
```

The review loop:
1. Sends the source job's result to the reviewer.
2. Reviewer responds with `{"verdict": "approved"|"changes_requested", "summary": "...", "findings": [...]}`.
3. If changes requested: findings go to the dev agent for fixes.
4. Dev fixes and responds. Reviewer re-reviews.
5. Repeats until approved or max rounds reached.

### Managing review sessions

```bash
# Check review progress
agp review-status <source_job_id>

# Diagnose a stuck review
agp review-diagnose <source_job_id>

# Resume a detached/interrupted review
agp review --resume <source_job_id>
```

## Agent Lifecycle

### Provision a new agent

```bash
# From a registered capability
agp up <capability_name>
agp up <capability_name> --agent-id my-custom-name
agp up <capability_name> --workspace /path/to/workdir
```

### Tear down an agent

```bash
# Idle agents
agp down <agent_id>

# Busy agents (cancels active jobs)
agp down <agent_id> --force
```

### Interrupt work

```bash
# Stop an agent's current job (agent stays alive, picks up next queued job)
agp interrupt <agent_id>

# Cancel a specific job
agp interrupt <job_id>

# Stop current job AND purge the queue
agp interrupt <agent_id> --purge
```

## Diagnostics

### Agent health

```bash
agp diagnose agent <agent_id>
```

Shows registration, heartbeat age, recent jobs, and runtime binding.

### Runtime health

```bash
agp diagnose runtime <runtime_id>
```

### Nudge an orchestrator

Send a human message to an orchestrator agent's terminal:

```bash
agp nudge <agent_id> "Please prioritize the auth fix"
```

### Diagnosing slow or stalled jobs

When a job appears stuck, **diagnose before attributing the stall to the agent.** Most stalls are caused by infrastructure — permission gates, TUI prompts, network delays — not agent quality.

```bash
# 1. Check what the agent is actually doing
agp status <job_id>
# ACTIVITY shows the semantic state: "working", "dismissing prompt",
# "blocked: requires login", "idle", or the last content line.
# LAST_SEEN shows heartbeat freshness — >30s suggests a real stall.

# 2. Deep-dive if needed
agp diagnose agent <agent_id>

# 3. Peek at the agent's live terminal (works for local and remote agents)
agp peek <agent_id>
agp peek <agent_id> --lines 50    # include scrollback
```

**Common causes of apparent stalls:**
- **Permission gate**: The runtime auto-dismisses these, but each one adds ~5-10s. Multiple consecutive gates (trust folder, tool approval) can look like a long stall.
- **Status bar misread**: Text like `⏵⏵ bypass permissions on (shift+tab to cycle)` is the TUI's permission mode indicator — it is always visible and does NOT mean the agent is stuck on a prompt.
- **Context window compaction**: Long sessions trigger compaction (`✻ Conversation compacted`) which pauses output for several seconds.
- **Network/API latency**: The model may take 30-60s on complex queries. Check LAST_SEEN — if heartbeats are fresh, the agent is alive.

**Do not conclude an agent is "unreliable" or "drifting" based solely on it not returning results in your expected time window.** Use `agp status` and `agp diagnose` to establish the actual cause first.

## Workflow Recipes

### Dev + Dual Review

```bash
# 1. Send implementation task
agp send dev-agent "Implement rate limiting on /api/submit" --detach
# -> job_impl

# 2. Wait for completion
agp wait job_impl --timeout 600

# 3. Send to two reviewers in parallel
agp send reviewer-a "Review the rate limiting implementation at src/api/submit.py. Check for correctness and edge cases." --detach
agp send reviewer-b "Review the rate limiting implementation at src/api/submit.py. Check for security and performance." --detach

# 4. Collect both reviews
agp result job_review_a
agp result job_review_b
```

### Iterative Fix Cycle

```bash
# 1. Dev does initial work
agp send dev-agent "Fix the memory leak in worker.py" --timeout 300
# -> job_fix

# 2. Review it
agp review job_fix reviewer-agent --dev dev-agent --diff --max-rounds 3
```

### Pipeline: Output of One Agent Feeds Another

```bash
# 1. Agent A produces analysis
agp send analyst-agent "Analyze error rates in the last 24h" --timeout 120
# -> job_analysis

# 2. Feed the result to agent B
agp result job_analysis | agp send dev-agent -
```

### Quick Status Check

```bash
# What's everyone doing?
agp ls

# Any failures recently?
agp jobs --status failed --limit 5

# Is the infra healthy?
agp health
```

## Tips

- **Job IDs are everywhere.** Every `send`, `reply`, and `review` returns a job ID. Save them.
- **`--detach` is your friend for parallel work.** Send multiple tasks with `--detach`, then collect results when you're ready.
- **Use `--timeout` for the CLI wait, `--timeout-seconds` for agent execution.** They're different: one controls your terminal, the other controls the agent runtime.
- **`agp result` defaults to transcript over result.** If you want the clean extracted output, use `--role result`. If you want the full session log, default is fine.
- **`agp status` works for both jobs and agents.** Pass a job ID or agent ID.
- **Review loops auto-detach if interrupted.** Resume with `agp review --resume <source_job_id>`.
- **Stdin with `-` works on both `send` and `reply`.** Pipe output from other tools directly into agents.
