# ── AGP / Skynet Makefile ─────────────────────────────────────────────
#
# Quick start:
#   1. cp .env.example .env        — fill in your API keys
#   2. make install-server          — install everything
#   3. make local-serve             — start local CP (SQLite, no infra)
#   4. make runtime                 — start a runtime (tmux + codex)
#   5. agp send agt_local "hello"   — send work
#
# Remote CP:
#   make runtime-remote             — connect local runtime to remote CP
#   agp send agt_local "hello" --server-url $AGP_REMOTE_SERVER_URL
#
# ──────────────────────────────────────────────────────────────────────

SHELL := /bin/bash
ROOT  := $(shell pwd)
PID_DIR := $(ROOT)/.skyops-pids
SUDO := $(shell if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then echo "sudo -n"; fi)

# ── Load .env file if present ─────────────────────────────────────────
# Place secrets and machine-specific config in .env (gitignored).
-include .env
export

# ── Prefer uv run ─────────────────────────────────────────────────────
UV := $(shell command -v uv 2>/dev/null)
ifdef UV
  RUN := uv run
else
  RUN :=
endif

# ── Defaults (override in .env or environment) ────────────────────────
AGP_HOST               ?= 0.0.0.0
AGP_PORT               ?= 7860
AGP_DATABASE_URL       ?= postgresql+psycopg://agp:agp@localhost:5432/agp
AGP_QUEUE_BACKEND      ?= redis
AGP_REDIS_URL          ?= redis://localhost:6379/0
AGP_ARTIFACT_BACKEND   ?= localfs
AGP_ARTIFACT_ROOT      ?= $(ROOT)/.agp-artifacts
AGP_LOG_ROOT           ?= $(ROOT)/.agp-logs

# Remote CP — where runtime-remote connects
AGP_REMOTE_SERVER_URL  ?= http://your-server.example.com:7860

# Runtime identity
AGP_RUNTIME_ID         ?= rtm_mac
AGP_RUNTIME_AGENT_ID   ?= agt_local

# Codex launch command. Defaults to the same local path that works when
# ncodex is started manually on this host; callers can still override it.
AGP_CODEX_CLI_COMMAND  ?= ncodex -a never -s danger-full-access

# ── Validation helpers ────────────────────────────────────────────────
define require_env
	@if [ -z "$($(1))" ]; then \
		echo "ERROR: $(1) is not set."; \
		echo "  Set it in .env or export it: export $(1)=..."; \
		echo "  See .env.example for reference."; \
		exit 1; \
	fi
endef

define require_provider_env
	@if [ -z "$(OPENAI_API_KEY)" ] && [ -z "$(OPENROUTER_API_KEY)" ]; then \
		echo "ERROR: set OPENAI_API_KEY or OPENROUTER_API_KEY."; \
		echo "  See .env.example for reference."; \
		exit 1; \
	fi
endef

# ── Install targets ──────────────────────────────────────────────────

.PHONY: install-server install-runtime

install-server: ## Install CP + infra + runtime deps (any OS)
	@bash scripts/install-server.sh

install-runtime: ## Install runtime only — no infra (any OS)
	@bash scripts/install-runtime.sh

# ── Local bare-metal targets (SQLite, no infra) ──────────────────────

.PHONY: local-reset local-initdb local-seed local-serve local-status

local-reset: ## Wipe local SQLite/log/artifact/checkpoint state
	@rm -f agp.db agp.db-wal agp.db-shm
	@rm -rf .agp-artifacts .agp-logs .agp-checkpoints
	@echo "Local state cleared."

local-initdb: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-initdb: export AGP_QUEUE_BACKEND=db
local-initdb: export AGP_ARTIFACT_BACKEND=localfs
local-initdb: ## Init local SQLite database
	@mkdir -p .agp-artifacts .agp-logs .agp-checkpoints
	$(RUN) agp initdb

local-seed: export AGP_SERVER_URL=http://127.0.0.1:$(AGP_PORT)
local-seed: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-seed: export AGP_QUEUE_BACKEND=db
local-seed: export AGP_ARTIFACT_BACKEND=localfs
local-seed: ## Seed capabilities and agents into local stack
	$(RUN) skyops db seed

local-serve: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-serve: export AGP_QUEUE_BACKEND=db
local-serve: export AGP_ARTIFACT_BACKEND=localfs
local-serve: ## Start local CP + sweepers (SQLite/memory)
	@mkdir -p $(PID_DIR) .agp-logs .agp-artifacts .agp-checkpoints
	@if lsof -nP -iTCP:$(AGP_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "ERROR: Port $(AGP_PORT) already in use."; \
		lsof -nP -iTCP:$(AGP_PORT) -sTCP:LISTEN; \
		exit 1; \
	fi
	@echo "Starting local control plane on :$(AGP_PORT)..."
	@$(RUN) agp serve > .agp-logs/control-plane.out 2>&1 & echo $$! > $(PID_DIR)/control-plane.pid
	@sleep 2
	@if ! curl -fsS http://127.0.0.1:$(AGP_PORT)/health >/dev/null 2>&1; then \
		echo "ERROR: Control plane failed to start. Check .agp-logs/control-plane.out"; \
		test -f $(PID_DIR)/control-plane.pid && kill "$$(cat $(PID_DIR)/control-plane.pid)" 2>/dev/null || true; \
		rm -f $(PID_DIR)/control-plane.pid; \
		exit 1; \
	fi
	@echo "Starting sweepers..."
	@$(RUN) agp sweep-loop --interval-seconds 5 > .agp-logs/lease-sweeper.out 2>&1 & echo $$! > $(PID_DIR)/lease-sweeper.pid
	@$(RUN) agp sweep-runtimes-loop --interval-seconds 10 > .agp-logs/runtime-sweeper.out 2>&1 & echo $$! > $(PID_DIR)/runtime-sweeper.pid
	@echo ""
	@echo "Local CP running at http://127.0.0.1:$(AGP_PORT)"
	@echo "Next: make runtime"

local-status: ## Show local stack health
	@echo "=== Processes ==="
	@found=0; \
	for pidfile in $(PID_DIR)/control-plane.pid $(PID_DIR)/lease-sweeper.pid $(PID_DIR)/runtime-sweeper.pid $(PID_DIR)/nudge-loop.pid; do \
		if [ -f $$pidfile ]; then \
			pid="$$(cat $$pidfile)"; \
			if ps -p "$$pid" -o pid=,ppid=,%cpu=,%mem=,command= >/dev/null 2>&1; then \
				if [ $$found -eq 0 ]; then printf "%-10s %-10s %-6s %-6s %s\n" PID PPID %CPU %MEM COMMAND; fi; \
				ps -p "$$pid" -o pid=,ppid=,%cpu=,%mem=,command=; \
				found=1; \
			fi; \
		fi; \
	done; \
	if [ $$found -eq 0 ]; then echo "(none running)"; fi
	@echo ""
	@echo "=== Health ==="
	@curl -s http://127.0.0.1:$(AGP_PORT)/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Local CP not reachable"

# ── Postgres/Redis targets (standard infrastructure) ─────────────────

.PHONY: initdb seed serve

initdb: ## Initialize database schema (requires running Postgres)
	$(RUN) agp initdb

seed: ## Seed capabilities and agents from skyops.toml
	$(RUN) skyops db seed

serve: ## Start CP + sweepers (bare-metal, Postgres/Redis)
	@mkdir -p .skyops-pids .agp-logs .agp-artifacts
	@echo "Starting control plane..."
	@$(RUN) agp serve &
	@sleep 2
	@echo "Starting sweepers..."
	@$(RUN) agp sweep-loop --interval-seconds 5 &
	@$(RUN) agp sweep-runtimes-loop --interval-seconds 10 &
	@echo ""
	@echo "CP running at http://$(AGP_HOST):$(AGP_PORT)"

nudge-loop: ## Start nudge delivery daemon for orc
	$(RUN) agp nudge-loop agt_orc --session agp-agt_orc

# ── Stop targets ─────────────────────────────────────────────────────

.PHONY: stop stop-cp stop-runtime stop-docker stop-kind

stop: stop-cp stop-runtime stop-docker stop-kind ## Stop everything

stop-cp: ## Stop control plane + sweepers
	@for pidfile in $(PID_DIR)/control-plane.pid $(PID_DIR)/lease-sweeper.pid $(PID_DIR)/runtime-sweeper.pid $(PID_DIR)/nudge-loop.pid; do \
		if [ -f $$pidfile ]; then rm -f $$pidfile; fi; \
	done
	@for port in $(AGP_PORT); do \
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

stop-docker: ## Stop docker compose stack
	@if command -v docker >/dev/null 2>&1; then \
		docker compose -f compose.phase3.yaml -p agp down -v --remove-orphans >/dev/null 2>&1 || true; \
	fi
	@echo "Docker stack stopped."

stop-kind: ## Delete kind clusters
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
	@curl -s http://127.0.0.1:$(AGP_PORT)/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "CP not reachable at :$(AGP_PORT)"

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

runtime: ## Start a local runtime (tmux + codex, connects to local CP)
	$(call require_provider_env)
	@echo "Starting runtime $(AGP_RUNTIME_ID) -> http://127.0.0.1:$(AGP_PORT) (agent=$(AGP_RUNTIME_AGENT_ID))"
	AGP_ARTIFACT_BACKEND=http \
	AGP_CODEX_TUI_MODE=true \
	AGP_CODEX_CLI_COMMAND="$(AGP_CODEX_CLI_COMMAND)" \
	AGP_CODEX_MAX_POLLS=240 \
	AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
	AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
	AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
	AGP_CODEX_IDLE_AFTER=5 \
	$(RUN) agp runtime-work-loop rtm_local \
		--server-url http://127.0.0.1:$(AGP_PORT) \
		--host-kind tmux \
		--adapter-kind codex \
		--agent-id agt_local

runtime-remote: ## Start a runtime connecting to remote CP
	$(call require_provider_env)
	$(call require_env,AGP_REMOTE_SERVER_URL)
	@echo "Starting runtime $(AGP_RUNTIME_ID) -> $(AGP_REMOTE_SERVER_URL) (agent=$(AGP_RUNTIME_AGENT_ID))"
	AGP_SERVER_URL="$(AGP_REMOTE_SERVER_URL)" \
	AGP_ARTIFACT_BACKEND=http \
	AGP_RUNTIME_TERMINAL_HOST_KIND=tmux \
	AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
	AGP_CODEX_TUI_MODE=true \
	AGP_TMUX_DEFAULT_CWD="$(ROOT)" \
	AGP_CODEX_CLI_COMMAND="$(AGP_CODEX_CLI_COMMAND)" \
	AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
	AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
	AGP_CODEX_IDLE_AFTER=5 \
	$(RUN) agp runtime-work-loop "$(AGP_RUNTIME_ID)" \
		--server-url "$(AGP_REMOTE_SERVER_URL)" \
		--host-kind tmux \
		--adapter-kind codex \
		--agent-id "$(AGP_RUNTIME_AGENT_ID)"

runtime-wezterm: ## Start a WezTerm runtime connecting to remote CP
	$(call require_provider_env)
	$(call require_env,AGP_REMOTE_SERVER_URL)
	@echo "Starting WezTerm runtime $(AGP_RUNTIME_ID) -> $(AGP_REMOTE_SERVER_URL) (agent=$(AGP_RUNTIME_AGENT_ID))"
	AGP_SERVER_URL="$(AGP_REMOTE_SERVER_URL)" \
	AGP_ARTIFACT_BACKEND=http \
	AGP_RUNTIME_TERMINAL_HOST_KIND=wezterm \
	AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
	AGP_CODEX_TUI_MODE=true \
	AGP_WEZTERM_DEFAULT_CWD="$(ROOT)" \
	AGP_CODEX_CLI_COMMAND="$(AGP_CODEX_CLI_COMMAND)" \
	AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
	AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
	AGP_CODEX_IDLE_AFTER=5 \
	$(RUN) agp runtime-work-loop "$(AGP_RUNTIME_ID)" \
		--server-url "$(AGP_REMOTE_SERVER_URL)" \
		--host-kind wezterm \
		--adapter-kind codex \
		--agent-id "$(AGP_RUNTIME_AGENT_ID)"

runtime-stop-remote: ## Stop local runtime worker process
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@echo "Runtime worker stopped."

runtime-clean-tmux: ## Kill tmux session for the runtime agent
	@if command -v tmux >/dev/null 2>&1; then \
		tmux kill-session -t "agp-$(AGP_RUNTIME_AGENT_ID)" 2>/dev/null || true; \
	fi
	@echo "tmux session cleaned."

runtime-clean-wezterm: ## Kill WezTerm panes for the runtime agent
	@if command -v wezterm >/dev/null 2>&1; then \
		AGP_RUNTIME_AGENT_ID="$(AGP_RUNTIME_AGENT_ID)" wezterm cli list --format json 2>/dev/null | python3 -c 'import json, os, subprocess, sys; raw = sys.stdin.read().strip(); panes = json.loads(raw) if raw else []; agent = "AGP:" + os.environ["AGP_RUNTIME_AGENT_ID"]; [subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(p["pane_id"])], check=False) for p in panes if p.get("pane_id") and agent in (p.get("tab_title") or "")]' || true; \
	fi
	@echo "WezTerm panes cleaned."

runtime-clean: runtime-stop-remote runtime-clean-tmux runtime-clean-wezterm ## Full runtime cleanup
	@echo "Runtime cleanup complete."

runtime-deploy: ## Generate deploy script for a remote runtime
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
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help
