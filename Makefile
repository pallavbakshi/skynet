# ── AGP / Skynet Makefile ─────────────────────────────────────────────
# Two install targets:
#   make install-server   — CP + infra + runtime deps (any OS)
#   make install-runtime  — Runtime only, no infra (any OS)
#
# Service targets (after install):
#   make serve            — Start CP + sweepers (bare-metal)
#   make runtime          — Start a runtime (tmux + codex)
#   make runtime-remote   — Start a remote runtime against Ubuntu CP
#   make runtime-wezterm  — Start a remote runtime via WezTerm against Ubuntu CP
#   make runtime-clean    — Stop runtime worker and clean terminal sessions
#   make local-reset      — Wipe local SQLite/log/artifact state
#   make local-initdb     — Init local SQLite bare-metal state
#   make local-serve      — Start local CP + sweepers with SQLite/memory
#   make stop             — Stop everything local (bare-metal + docker + kind)
#   make status           — Show what's running
# ──────────────────────────────────────────────────────────────────────

SHELL := /bin/bash
ROOT  := $(shell pwd)
PID_DIR := $(ROOT)/.skyops-pids
SUDO := $(shell if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then echo "sudo -n"; fi)

# Prefer uv run, fall back to direct command
UV := $(shell command -v uv 2>/dev/null)
ifdef UV
  RUN := uv run
else
  RUN :=
endif

# ── Env defaults (override with env vars or .env file) ────────────────
export AGP_HOST            ?= 0.0.0.0
export AGP_PORT            ?= 7860
export AGP_DATABASE_URL    ?= postgresql+psycopg://agp:agp@localhost:5432/agp
export AGP_QUEUE_BACKEND   ?= redis
export AGP_REDIS_URL       ?= redis://localhost:6379/0
export AGP_ARTIFACT_BACKEND ?= localfs
export AGP_ARTIFACT_ROOT   ?= $(ROOT)/.agp-artifacts
export AGP_LOG_ROOT        ?= $(ROOT)/.agp-logs
export AGP_REMOTE_SERVER_URL ?= http://your-server.example.com:7860
export AGP_RUNTIME_ID      ?= rtm_mac
export AGP_RUNTIME_AGENT_ID ?= agt_local

# ── Install targets ──────────────────────────────────────────────────

.PHONY: install-server install-runtime

install-server: ## Install CP + infra + runtime deps (any OS)
	@bash scripts/install-server.sh

install-runtime: ## Install runtime only — no infra (any OS)
	@bash scripts/install-runtime.sh

# ── Service targets (bare-metal, no docker) ───────────────────────────

.PHONY: serve stop stop-cp stop-runtime stop-docker stop-kind status initdb seed local-reset local-initdb local-seed local-serve local-status

initdb: ## Initialize database schema
	$(RUN) agp initdb

seed: ## Seed capabilities and agents from skyops.toml
	$(RUN) skyops db seed

local-reset: ## Wipe local SQLite/log/artifact/checkpoint state
	@rm -f agp.db agp.db-wal agp.db-shm
	@rm -rf .agp-artifacts .agp-logs .agp-checkpoints
	@echo "Local state cleared."

local-initdb: export AGP_HOST=0.0.0.0
local-initdb: export AGP_PORT=7860
local-initdb: export AGP_DATABASE_URL=sqlite+pysqlite:///${ROOT}/agp.db
local-initdb: export AGP_QUEUE_BACKEND=db
local-initdb: export AGP_ARTIFACT_BACKEND=localfs
local-initdb: export AGP_ARTIFACT_ROOT=${ROOT}/.agp-artifacts
local-initdb: export AGP_LOG_ROOT=${ROOT}/.agp-logs
local-initdb: export AGP_CHECKPOINT_ROOT=${ROOT}/.agp-checkpoints
local-initdb: ## Initialize bare-metal local SQLite state
	@mkdir -p .agp-artifacts .agp-logs .agp-checkpoints
	$(RUN) agp initdb

local-seed: export AGP_HOST=0.0.0.0
local-seed: export AGP_PORT=7860
local-seed: export AGP_SERVER_URL=http://127.0.0.1:7860
local-seed: export AGP_DATABASE_URL=sqlite+pysqlite:///${ROOT}/agp.db
local-seed: export AGP_QUEUE_BACKEND=db
local-seed: export AGP_ARTIFACT_BACKEND=localfs
local-seed: export AGP_ARTIFACT_ROOT=${ROOT}/.agp-artifacts
local-seed: export AGP_LOG_ROOT=${ROOT}/.agp-logs
local-seed: export AGP_CHECKPOINT_ROOT=${ROOT}/.agp-checkpoints
local-seed: ## Seed capabilities and agents into the local bare-metal stack
	$(RUN) skyops db seed

local-serve: export AGP_HOST=0.0.0.0
local-serve: export AGP_PORT=7860
local-serve: export AGP_DATABASE_URL=sqlite+pysqlite:///${ROOT}/agp.db
local-serve: export AGP_QUEUE_BACKEND=db
local-serve: export AGP_ARTIFACT_BACKEND=localfs
local-serve: export AGP_ARTIFACT_ROOT=${ROOT}/.agp-artifacts
local-serve: export AGP_LOG_ROOT=${ROOT}/.agp-logs
local-serve: export AGP_CHECKPOINT_ROOT=${ROOT}/.agp-checkpoints
local-serve: ## Start local CP + sweepers with SQLite/memory defaults
	@mkdir -p $(PID_DIR) .agp-logs .agp-artifacts .agp-checkpoints
	@if lsof -nP -iTCP:$${AGP_PORT} -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port $${AGP_PORT} is already in use."; \
		lsof -nP -iTCP:$${AGP_PORT} -sTCP:LISTEN; \
		exit 1; \
	fi
	@echo "Starting local control plane..."
	@$(RUN) agp serve > .agp-logs/control-plane.out 2>&1 & echo $$! > $(PID_DIR)/control-plane.pid
	@sleep 2
	@if ! curl -fsS http://127.0.0.1:$${AGP_PORT}/health >/dev/null 2>&1; then \
		echo "Control plane failed to start."; \
		test -f .agp-logs/control-plane.out && tail -n 50 .agp-logs/control-plane.out || true; \
		test -f $(PID_DIR)/control-plane.pid && kill "$$(cat $(PID_DIR)/control-plane.pid)" 2>/dev/null || true; \
		rm -f $(PID_DIR)/control-plane.pid; \
		exit 1; \
	fi
	@echo "Starting lease sweeper..."
	@$(RUN) agp sweep-loop --interval-seconds 5 > .agp-logs/lease-sweeper.out 2>&1 & echo $$! > $(PID_DIR)/lease-sweeper.pid
	@echo "Starting runtime sweeper..."
	@$(RUN) agp sweep-runtimes-loop --interval-seconds 10 > .agp-logs/runtime-sweeper.out 2>&1 & echo $$! > $(PID_DIR)/runtime-sweeper.pid
	@echo ""
	@echo "Local CP running at http://127.0.0.1:$${AGP_PORT}"
	@echo "Use 'make stop' to shut down."

local-status: export AGP_PORT=7860
local-status: ## Show health for the local bare-metal stack
	@echo "=== Local AGP Processes ==="
	@found=0; \
	for pidfile in $(PID_DIR)/control-plane.pid $(PID_DIR)/lease-sweeper.pid $(PID_DIR)/runtime-sweeper.pid $(PID_DIR)/nudge-loop.pid; do \
		if [ -f $$pidfile ]; then \
			pid="$$(cat $$pidfile)"; \
			if ps -p "$$pid" -o pid=,ppid=,%cpu=,%mem=,command= >/dev/null 2>&1; then \
				if [ $$found -eq 0 ]; then \
					printf "%-10s %-10s %-6s %-6s %s\n" PID PPID %CPU %MEM COMMAND; \
				fi; \
				ps -p "$$pid" -o pid=,ppid=,%cpu=,%mem=,command=; \
				found=1; \
			fi; \
		fi; \
	done; \
	if [ $$found -eq 0 ]; then echo "(none running)"; fi
	@echo ""
	@echo "=== Local Health ==="
	@curl -s http://127.0.0.1:$${AGP_PORT}/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Local CP not reachable"

serve: ## Start CP + sweepers in background (bare-metal)
	@mkdir -p .skyops-pids .agp-logs .agp-artifacts
	@echo "Starting control plane..."
	@$(RUN) agp serve &
	@sleep 2
	@echo "Starting lease sweeper..."
	@$(RUN) agp sweep-loop --interval-seconds 5 &
	@echo "Starting runtime sweeper..."
	@$(RUN) agp sweep-runtimes-loop --interval-seconds 10 &
	@echo ""
	@echo "CP running at http://$${AGP_HOST}:$${AGP_PORT}"
	@echo "Use 'make stop' to shut down."

nudge-loop: ## Start nudge delivery daemon for orc
	$(RUN) agp nudge-loop agt_orc --session agp-agt_orc

stop: stop-cp stop-runtime stop-docker stop-kind ## Stop everything local (bare-metal + docker + kind)

stop-cp: ## Stop control plane + sweepers + nudge daemon
	@for pidfile in $(PID_DIR)/control-plane.pid $(PID_DIR)/lease-sweeper.pid $(PID_DIR)/runtime-sweeper.pid $(PID_DIR)/nudge-loop.pid; do \
		if [ -f $$pidfile ]; then \
			rm -f $$pidfile; \
		fi; \
	done
	@for port in $${AGP_PORT:-7860}; do \
		pids="$$(lsof -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true)"; \
		if [ -n "$$pids" ]; then \
			$(SUDO) kill $$pids 2>/dev/null || kill $$pids 2>/dev/null || true; \
			sleep 1; \
			pids="$$(lsof -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true)"; \
			if [ -n "$$pids" ]; then $(SUDO) kill -9 $$pids 2>/dev/null || kill -9 $$pids 2>/dev/null || true; fi; \
		fi; \
	done
	@rm -f $(PID_DIR)/control-plane.pid $(PID_DIR)/lease-sweeper.pid $(PID_DIR)/runtime-sweeper.pid $(PID_DIR)/nudge-loop.pid
	@echo "CP stopped."

stop-runtime: ## Stop runtime worker
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@echo "Runtime stopped."

stop-docker: ## Stop local AGP docker compose stack if present
	@if command -v docker >/dev/null 2>&1; then \
		docker compose -f compose.phase3.yaml -p agp down -v --remove-orphans >/dev/null 2>&1 || true; \
	fi
	@echo "Docker stack stopped."

stop-kind: ## Delete common local AGP kind clusters if present
	@if command -v kind >/dev/null 2>&1; then \
		for cluster in agp-phase3 agp-phase3-debug; do \
			kind get clusters 2>/dev/null | grep -qx "$$cluster" && kind delete cluster --name "$$cluster" >/dev/null 2>&1 || true; \
		done; \
	fi
	@rm -f .kubeconfig-kind-agp-phase3 .kubeconfig-kind-agp-phase3-debug .kind-agp-phase3.yaml
	@echo "Kind clusters stopped."

status: ## Show running services
	@echo "=== AGP Processes ==="
	@ps aux | head -1
	@ps aux | grep '[a]gp' || echo "(none running)"
	@echo ""
	@echo "=== Health ==="
	@curl -s http://127.0.0.1:$${AGP_PORT}/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "CP not reachable"

# ── Docker targets ───────────────────────────────────────────────────

.PHONY: up down ps

up: ## Start full stack via docker compose
	docker compose -f compose.phase3.yaml -p agp up -d --build

down: ## Stop docker stack and remove volumes
	docker compose -f compose.phase3.yaml -p agp down -v --remove-orphans

ps: ## Show docker compose status
	docker compose -f compose.phase3.yaml -p agp ps

# ── Runtime targets ──────────────────────────────────────────────────

.PHONY: runtime runtime-remote runtime-wezterm runtime-stop-remote runtime-clean-tmux runtime-clean-wezterm runtime-clean runtime-deploy

runtime: ## Start a local runtime (tmux + codex)
	OPENROUTER_API_KEY="$${OPENROUTER_API_KEY}" \
	AGP_ARTIFACT_BACKEND=http \
	AGP_CODEX_TUI_MODE=true \
	AGP_CODEX_CLI_COMMAND="ncodex -m openai/gpt-5.3-codex -a never -s danger-full-access" \
	AGP_CODEX_MAX_POLLS=240 \
	AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
	AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
	AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
	AGP_CODEX_IDLE_AFTER=5 \
	$(RUN) agp runtime-work-loop rtm_local \
		--server-url http://127.0.0.1:$${AGP_PORT} \
		--host-kind tmux \
		--adapter-kind codex \
		--agent-id agt_local

runtime-remote: ## Start a remote tmux runtime against the hard-coded Ubuntu control plane
	OPENROUTER_API_KEY="$${OPENROUTER_API_KEY}" \
	AGP_SERVER_URL="$${AGP_REMOTE_SERVER_URL}" \
	AGP_ARTIFACT_BACKEND=http \
	AGP_RUNTIME_TERMINAL_HOST_KIND=tmux \
	AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
	AGP_CODEX_TUI_MODE=true \
	AGP_TMUX_DEFAULT_CWD="$(ROOT)" \
	AGP_CODEX_CLI_COMMAND="codex --full-auto" \
	$(RUN) agp runtime-work-loop "$${AGP_RUNTIME_ID}" \
		--server-url "$${AGP_REMOTE_SERVER_URL}" \
		--host-kind tmux \
		--adapter-kind codex \
		--agent-id "$${AGP_RUNTIME_AGENT_ID}"

runtime-wezterm: ## Start a remote WezTerm runtime against the hard-coded Ubuntu control plane
	OPENROUTER_API_KEY="$${OPENROUTER_API_KEY}" \
	AGP_SERVER_URL="$${AGP_REMOTE_SERVER_URL}" \
	AGP_ARTIFACT_BACKEND=http \
	AGP_RUNTIME_TERMINAL_HOST_KIND=wezterm \
	AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
	AGP_CODEX_TUI_MODE=true \
	AGP_WEZTERM_DEFAULT_CWD="$(ROOT)" \
	AGP_CODEX_CLI_COMMAND="codex --full-auto" \
	$(RUN) agp runtime-work-loop "$${AGP_RUNTIME_ID}" \
		--server-url "$${AGP_REMOTE_SERVER_URL}" \
		--host-kind wezterm \
		--adapter-kind codex \
		--agent-id "$${AGP_RUNTIME_AGENT_ID}"

runtime-stop-remote: ## Stop local runtime worker process on the current machine
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@echo "Remote runtime worker stopped."

runtime-clean-tmux: ## Kill tmux session for the configured AGP runtime agent
	@if command -v tmux >/dev/null 2>&1; then \
		tmux kill-session -t "agp-$${AGP_RUNTIME_AGENT_ID}" 2>/dev/null || true; \
	fi
	@echo "tmux runtime session cleaned."

runtime-clean-wezterm: ## Kill WezTerm panes titled AGP:<agent_id>
	@if command -v wezterm >/dev/null 2>&1; then \
		AGP_RUNTIME_AGENT_ID="$${AGP_RUNTIME_AGENT_ID}" wezterm cli list --format json 2>/dev/null | python3 -c 'import json, os, subprocess, sys; agent = "AGP:" + os.environ["AGP_RUNTIME_AGENT_ID"]; panes = json.load(sys.stdin); [subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(p["pane_id"])], check=False) for p in panes if p.get("pane_id") and agent in (p.get("tab_title") or "")]' || true; \
	fi
	@echo "WezTerm runtime panes cleaned."

runtime-clean: runtime-stop-remote runtime-clean-tmux runtime-clean-wezterm ## Stop runtime worker and clean tmux/WezTerm sessions
	@echo "Remote runtime cleanup complete."

runtime-deploy: ## Generate deploy command for a remote runtime
	@$(RUN) skyops runtime deploy rtm_remote --format script

# ── Dev/test targets ─────────────────────────────────────────────────

.PHONY: test lint

test: ## Run test suite
	$(RUN) python -m pytest tests/ -x -q

lint: ## Run linter
	$(RUN) python -m ruff check src/ tests/

# ── Help ─────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
