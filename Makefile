# ── AGP / Skynet Makefile ─────────────────────────────────────────────
#
# Quick start (local, no infra):
#   make local-up                    — reset + init + serve CP
#   make runtime                     — start a runtime (agent self-registers)
#   agp send coder-1 "hello"         — send work
#   make local-status                — check what's running
#   make local-down                  — stop everything
#
# Docker stack:
#   make up                          — start full docker compose stack
#   make down                        — stop + wipe volumes
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

# Runtime identity (self-registration model)
AGP_RUNTIME_ID         ?= rtm_local
AGP_RUNTIME_AGENT_ID   ?= agt_local
AGP_RUNTIME_CAPS       ?= code,python

# Provider auto-detection: prefer OPENAI_API_KEY, fall back to OPENROUTER_API_KEY
_RUNTIME_API_KEY       := $(or $(OPENAI_API_KEY),$(OPENROUTER_API_KEY))
_RUNTIME_BASE_URL      := $(or $(OPENAI_BASE_URL),$(if $(OPENROUTER_API_KEY),https://openrouter.ai/api/v1))

# Codex profile and launch command.
#   make runtime                              — OAuth / codex default credentials
#   make runtime CODEX_PROFILE=openrouter     — OpenRouter via OPENROUTER_API_KEY
#   make runtime CODEX_PROFILE=apikey         — direct OpenAI via OPENAI_API_KEY
CODEX_PROFILE          ?=

ifneq ($(CODEX_PROFILE),)
_PROVIDER_ENV         := OPENAI_API_KEY="$(_RUNTIME_API_KEY)" OPENAI_BASE_URL="$(_RUNTIME_BASE_URL)" OPENROUTER_API_KEY="$(OPENROUTER_API_KEY)"
endif
AGP_CODEX_CLI_COMMAND  ?= codex$(if $(CODEX_PROFILE), -p $(CODEX_PROFILE)) --dangerously-bypass-approvals-and-sandbox

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
		echo "INFO: no OPENAI_API_KEY or OPENROUTER_API_KEY set — using codex OAuth credentials."; \
	fi
endef

# ── Install targets ──────────────────────────────────────────────────

.PHONY: install-server install-runtime install-docker

install-server: ## Install CP + infra + runtime deps (any OS)
	@bash scripts/install-server.sh

install-runtime: ## Install runtime only — no infra (any OS)
	@bash scripts/install-runtime.sh

install-docker: ## Install Docker Engine + Compose for `make up`
	@bash scripts/install-docker.sh

# ── Local bare-metal targets (SQLite, no infra) ──────────────────────

.PHONY: local-reset local-initdb local-serve local-up local-down local-status

local-reset: ## Wipe local SQLite/log/artifact/checkpoint state
	@rm -f agp.db agp.db-wal agp.db-shm
	@rm -rf .agp-artifacts .agp-logs .agp-checkpoints
	@echo "Local state cleared."

local-initdb: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-initdb: export AGP_QUEUE_BACKEND=delivery_table
local-initdb: export AGP_ARTIFACT_BACKEND=localfs
local-initdb: ## Init local SQLite database
	@mkdir -p .agp-artifacts .agp-logs .agp-checkpoints
	$(RUN) agp initdb

local-serve: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-serve: export AGP_QUEUE_BACKEND=delivery_table
local-serve: export AGP_ARTIFACT_BACKEND=localfs
local-serve: ## Start local CP + sweepers (SQLite, no infra)
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
	@echo "Agents self-register — no seeding needed."
	@echo "Next: make runtime"

local-up: local-reset local-initdb local-serve ## Clean start: reset + init + serve (one command)

local-down: stop-cp stop-runtime ## Stop local CP + any running runtimes
	@echo "Local stack stopped."

local-status: ## Show local stack health and registered agents
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
	@echo ""
	@echo "=== Agents ==="
	@curl -s http://127.0.0.1:$(AGP_PORT)/agents 2>/dev/null | python3 -c \
		"import sys,json; d=json.load(sys.stdin); items=d.get('data',{}).get('items',[]); \
		[print(f'  {a[\"agent_id\"]:<20s} {str(\",\".join(a.get(\"capabilities\",[]))):<20s} {a[\"status\"]}') for a in items] \
		or print('  (none registered)')" 2>/dev/null || echo "  CP not reachable"

# ── Postgres/Redis targets (standard infrastructure) ─────────────────

.PHONY: initdb serve

initdb: ## Initialize database schema (requires running Postgres)
	$(RUN) agp initdb

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

stop-cp: ## Stop local control plane + sweepers (does not touch Docker)
	@for pidfile in $(PID_DIR)/control-plane.pid $(PID_DIR)/lease-sweeper.pid $(PID_DIR)/runtime-sweeper.pid $(PID_DIR)/nudge-loop.pid; do \
		if [ -f $$pidfile ]; then \
			pid="$$(cat $$pidfile)"; \
			kill "$$pid" 2>/dev/null || true; \
			rm -f $$pidfile; \
		fi; \
	done
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp (serve|sweep)/ {print $$1}'); do \
		kill $$pid 2>/dev/null || true; \
	done
	@echo "CP stopped."

stop-runtime: ## Stop runtime worker
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@echo "Runtime stopped."

stop-docker: ## Stop docker compose stack
	@if command -v docker >/dev/null 2>&1; then \
		docker compose -p agp down -v --remove-orphans >/dev/null 2>&1 || true; \
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

status: ## Show running services, health, and agents
	@_local=http://127.0.0.1:$(AGP_PORT); \
	_remote=$(AGP_REMOTE_SERVER_URL); \
	_url=""; _loc=""; \
	if curl -sf "$$_local/ops/health" >/dev/null 2>&1; then _url="$$_local"; _loc="LOCAL"; \
	elif curl -sf "$$_remote/ops/health" >/dev/null 2>&1; then _url="$$_remote"; _loc="REMOTE"; fi; \
	\
	echo "=== Control Plane ==="; \
	if [ -n "$$_url" ]; then \
		echo "  location : $$_loc — $$_url"; \
		curl -s "$$_url/ops/health" | python3 -c \
			"import sys,json; d=json.load(sys.stdin)['data']; \
			a=d['agents']; j=d['jobs']; \
			print(f'  agents   : idle={a[\"idle\"]}  busy={a[\"busy\"]}  draining={a[\"draining\"]}'); \
			print(f'  jobs     : running={j[\"running\"]}  queued={j[\"queued\"]}  completed={j[\"completed\"]}  failed={j[\"failed\"]}')"; \
	else \
		echo "  NOT REACHABLE (tried local $$_local and remote $$_remote)"; \
	fi; \
	\
	echo ""; \
	echo "=== Docker Containers (this machine) ==="; \
	if docker compose -p agp ps --status running 2>/dev/null | grep -q agp; then \
		docker compose -p agp ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null; \
	else \
		echo "  (none)"; \
	fi; \
	\
	echo ""; \
	echo "=== Runtime Processes (this machine) ==="; \
	ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ { \
		pid=$$1; rt="?"; ag="?"; url="?"; \
		for(i=2;i<=NF;i++) { \
			if($$i=="runtime-work-loop") rt=$$(i+1); \
			if($$i=="--agent-id") ag=$$(i+1); \
			if($$i=="--server-url") url=$$(i+1); \
		} \
		loc=(url ~ /127\.0\.0\.1/) ? "LOCAL CP" : "REMOTE CP"; \
		printf "  %-12s  agent=%-15s  → %s (%s)\n", rt, ag, url, loc \
	}' | sort -u || echo "  (none)"; \
	\
	echo ""; \
	echo "=== Registered Agents ==="; \
	if [ -n "$$_url" ]; then \
		curl -s "$$_url/agents" | python3 -c \
			"import sys,json; d=json.load(sys.stdin); items=d.get('data',{}).get('items',[]); \
			[print(f'  {a[\"agent_id\"]:<20s} {str(\",\".join(a.get(\"capabilities\",[]))):<20s} {a[\"status\"]}') for a in items] \
			or print('  (none registered)')"; \
	else \
		echo "  (CP not reachable)"; \
	fi

# ── Docker targets ───────────────────────────────────────────────────

.PHONY: up down ps

up: ## Start full stack via docker compose
	docker compose -p agp up -d --build

down: ## Stop docker stack and remove volumes
	docker compose -p agp down -v --remove-orphans

ps: ## Show docker compose status
	docker compose -p agp ps

# ── Runtime targets ──────────────────────────────────────────────────

.PHONY: runtime runtime-remote runtime-wezterm runtime-stop-remote runtime-clean-tmux runtime-clean-wezterm runtime-clean runtime-deploy

runtime: ## Start a local runtime (agent self-registers with CP)
	$(call require_provider_env)
	@echo "Starting runtime $(AGP_RUNTIME_ID) -> http://127.0.0.1:$(AGP_PORT) (agent=$(AGP_RUNTIME_AGENT_ID), caps=$(AGP_RUNTIME_CAPS))"
	$(_PROVIDER_ENV) \
	AGP_ARTIFACT_BACKEND=http \
	AGP_CODEX_TUI_MODE=true \
	AGP_CODEX_CLI_COMMAND="$(AGP_CODEX_CLI_COMMAND)" \
	AGP_CODEX_MAX_POLLS=240 \
	AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
	AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
	AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
	AGP_CODEX_IDLE_AFTER=5 \
	$(RUN) agp runtime-work-loop $(AGP_RUNTIME_ID) \
		--server-url http://127.0.0.1:$(AGP_PORT) \
		--host-kind tmux \
		--adapter-kind codex \
		--agent-id $(AGP_RUNTIME_AGENT_ID) \
		--capabilities $(AGP_RUNTIME_CAPS)

runtime-remote: ## Start a runtime connecting to remote CP
	$(call require_provider_env)
	$(call require_env,AGP_REMOTE_SERVER_URL)
	@echo "Starting runtime $(AGP_RUNTIME_ID) -> $(AGP_REMOTE_SERVER_URL) (agent=$(AGP_RUNTIME_AGENT_ID), caps=$(AGP_RUNTIME_CAPS))"
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
		--agent-id "$(AGP_RUNTIME_AGENT_ID)" \
		--capabilities $(AGP_RUNTIME_CAPS)

runtime-wezterm: ## Start a WezTerm runtime connecting to remote CP
	$(call require_provider_env)
	$(call require_env,AGP_REMOTE_SERVER_URL)
	@echo "Starting WezTerm runtime $(AGP_RUNTIME_ID) -> $(AGP_REMOTE_SERVER_URL) (agent=$(AGP_RUNTIME_AGENT_ID), caps=$(AGP_RUNTIME_CAPS))"
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
		--agent-id "$(AGP_RUNTIME_AGENT_ID)" \
		--capabilities $(AGP_RUNTIME_CAPS)

runtime-stop-remote: ## Stop local runtime worker process
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@echo "Runtime worker stopped."

runtime-clean-tmux: ## Kill all agp-* tmux sessions (or one if AGP_RUNTIME_AGENT_ID is set)
	@if command -v tmux >/dev/null 2>&1; then \
		if [ "$(AGP_RUNTIME_AGENT_ID)" != "agt_local" ]; then \
			tmux kill-session -t "agp-$(AGP_RUNTIME_AGENT_ID)" 2>/dev/null || true; \
		else \
			for sess in $$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^agp-'); do \
				tmux kill-session -t "$$sess" 2>/dev/null || true; \
			done; \
		fi; \
	fi
	@echo "tmux sessions cleaned."

runtime-clean-wezterm: ## Kill WezTerm panes for the runtime agent
	@if command -v wezterm >/dev/null 2>&1; then \
		AGP_RUNTIME_AGENT_ID="$(AGP_RUNTIME_AGENT_ID)" wezterm cli list --format json 2>/dev/null | python3 -c 'import json, os, subprocess, sys; raw = sys.stdin.read().strip(); panes = json.loads(raw) if raw else []; agent = "AGP:" + os.environ["AGP_RUNTIME_AGENT_ID"]; [subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(p["pane_id"])], check=False) for p in panes if p.get("pane_id") and agent in (p.get("tab_title") or "")]' || true; \
	fi
	@echo "WezTerm panes cleaned."

runtime-clean: ## Tear down ALL agents on the CP + kill local processes and sessions
	@echo "Tearing down all agents registered with CP..."
	@_url=""; \
	for u in http://127.0.0.1:$(AGP_PORT) $(AGP_REMOTE_SERVER_URL); do \
		if curl -sf "$$u/ops/health" >/dev/null 2>&1; then _url="$$u"; break; fi; \
	done; \
	if [ -n "$$_url" ]; then \
		for agent_id in $$(curl -s "$$_url/agents" | python3 -c "import sys,json; [print(a['agent_id']) for a in json.load(sys.stdin).get('data',{}).get('items',[])]" 2>/dev/null); do \
			echo "  agp down --force $$agent_id"; \
			$(RUN) agp down "$$agent_id" --force --server-url "$$_url" 2>/dev/null || true; \
		done; \
	else \
		echo "  (CP not reachable — skipping agent teardown)"; \
	fi
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@if command -v tmux >/dev/null 2>&1; then \
		for sess in $$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^agp-'); do \
			tmux kill-session -t "$$sess" 2>/dev/null || true; \
		done; \
	fi
	@if command -v wezterm >/dev/null 2>&1; then \
		wezterm cli list --format json 2>/dev/null | python3 -c 'import json, subprocess, sys; raw = sys.stdin.read().strip(); panes = json.loads(raw) if raw else []; [subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(p["pane_id"])], check=False) for p in panes if "AGP:" in (p.get("tab_title") or "")]' || true; \
	fi
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
