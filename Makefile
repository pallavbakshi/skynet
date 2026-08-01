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
ADAPTER_KIND            ?= codex

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
AGP_CLAUDE_CODE_CLI_COMMAND ?= $(shell command -v claude 2>/dev/null || echo claude)

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
	@$(RUN) python -c "from agp._local_state import stop_local_control_plane; pids = stop_local_control_plane(); print('Stopped local control plane pid(s): ' + ', '.join(str(pid) for pid in pids)) if pids else None"
	@rm -f agp.db agp.db-wal agp.db-shm .agp-crash
	@rm -rf .agp-artifacts .agp-logs .agp-checkpoints
	@echo "Local state cleared."

local-initdb: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-initdb: export AGP_QUEUE_BACKEND=delivery_table
local-initdb: export AGP_ARTIFACT_BACKEND=localfs
local-initdb: ## Init local SQLite database
	@$(RUN) python -c "from agp._local_state import ensure_local_control_plane_stopped; ensure_local_control_plane_stopped()"
	@mkdir -p .agp-artifacts .agp-logs .agp-checkpoints
	$(RUN) agp initdb

local-serve: export AGP_DATABASE_URL=sqlite+pysqlite:///$(ROOT)/agp.db
local-serve: export AGP_QUEUE_BACKEND=delivery_table
local-serve: export AGP_ARTIFACT_BACKEND=localfs
local-serve: export AGP_ENFORCE_SQLITE_RUNTIME_GUARD=1
local-serve: ## Start local CP + sweepers (SQLite, no infra)
	@mkdir -p $(PID_DIR) .agp-logs .agp-artifacts .agp-checkpoints
	@if lsof -nP -iTCP:$(AGP_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "ERROR: Port $(AGP_PORT) already in use."; \
		lsof -nP -iTCP:$(AGP_PORT) -sTCP:LISTEN; \
		exit 1; \
	fi
	@echo "Starting local control plane on :$(AGP_PORT)..."
	@nohup $(RUN) agp serve > .agp-logs/control-plane.out 2>&1 < /dev/null & echo $$! > $(PID_DIR)/control-plane.pid
	@echo "Waiting for health check..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		if curl -fsS --max-time 2 http://127.0.0.1:$(AGP_PORT)/health >/dev/null 2>&1; then \
			break; \
		fi; \
		if [ $$i -eq 10 ]; then \
			echo "ERROR: Control plane failed to start within 10s. Check .agp-logs/control-plane.out"; \
			test -f $(PID_DIR)/control-plane.pid && kill "$$(cat $(PID_DIR)/control-plane.pid)" 2>/dev/null || true; \
			rm -f $(PID_DIR)/control-plane.pid; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo "Starting sweepers..."
	@nohup $(RUN) agp sweep-loop --interval-seconds 5 > .agp-logs/lease-sweeper.out 2>&1 < /dev/null & echo $$! > $(PID_DIR)/lease-sweeper.pid
	@nohup $(RUN) agp sweep-runtimes-loop --interval-seconds 10 > .agp-logs/runtime-sweeper.out 2>&1 < /dev/null & echo $$! > $(PID_DIR)/runtime-sweeper.pid
	@echo ""
	@echo "Local CP running at http://127.0.0.1:$(AGP_PORT)"
	@echo "Agents self-register — no seeding needed."
	@echo "Next: make runtime"

local-restart: stop-runtime stop-cp ## Restart CP preserving existing DB + agent state
	@if [ ! -f agp.db ]; then \
		echo "ERROR: No existing database found. Run 'make local-up' for a clean start."; \
		exit 1; \
	fi
	@python3 -c "import sqlite3; c=sqlite3.connect('agp.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" 2>/dev/null && echo "WAL checkpointed." || true
	@mkdir -p .agp-artifacts .agp-logs .agp-checkpoints
	@rm -f .agp-crash
	@$(MAKE) local-serve

local-up: stop-runtime local-reset local-initdb local-serve ## Clean start: stop runtimes + reset + init + serve

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
	@curl -s --max-time 5 http://127.0.0.1:$(AGP_PORT)/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Local CP not reachable"
	@echo ""
	@echo "=== Agents ==="
	@curl -s --max-time 5 http://127.0.0.1:$(AGP_PORT)/agents 2>/dev/null | python3 -c \
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

.PHONY: stop stop-cp stop-runtime stop-docker stop-kind restart

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
	@# Kill runtime-work-loop processes and their parent uv/make wrappers
	@for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
	done
	@for pid in $$(ps -eo pid=,args= | awk '/[u]v run agp runtime-work-loop/ {print $$1}'); do \
		kill $$pid 2>/dev/null || true; \
	done
	@for pid in $$(ps -eo pid=,args= | awk '/[m]ake runtime AGP_RUNTIME_ID/ {print $$1}'); do \
		kill $$pid 2>/dev/null || true; \
	done
	@# Wait for processes to fully exit (avoids exit code 144 on restart)
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		if ! ps -eo args= 2>/dev/null | grep -q '[a]gp runtime-work-loop'; then break; fi; \
		sleep 0.5; \
	done
	@echo "Runtime stopped."

restart-runtime: ## Restart CP + runtime workers (re-reads code changes, preserves DB)
	@# 1. Discover running runtimes BEFORE stopping them (sweeper deletes agents on death)
	@_agents=$$(ps -eo args= 2>/dev/null | awk '/[a]gp runtime-work-loop/ { \
		for(i=1;i<=NF;i++) { if($$i=="--agent-id") print $$(i+1) } \
	}' | sort -u); \
	if [ -z "$$_agents" ]; then \
		echo "No running runtimes found. Start them manually:"; \
		echo "  make claude-dev &"; \
		echo "  make claude-reviewer &"; \
		exit 0; \
	fi; \
	echo "1/7 Discovering agents: $$_agents"; \
	echo "2/7 Stopping runtimes..."; \
	for pid in $$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ {print $$1}'); do \
		kill $$pid 2>/dev/null || true; \
	done; \
	for i in 1 2 3 4 5 6 7 8 9 10; do \
		ps -eo args= 2>/dev/null | grep -q '[a]gp runtime-work-loop' || break; \
		sleep 0.5; \
	done; \
	echo "3/7 Clearing old runtime state..."; \
	$(MAKE) runtime-clean-state; \
	echo "4/7 Stopping control plane..."; \
	$(MAKE) stop-cp; \
	echo "5/7 Reinstalling editable package..."; \
	uv pip install -e . -q 2>&1 | tail -1; \
	echo "6/7 Starting control plane..."; \
	$(MAKE) local-serve; \
	echo "7/7 Restarting runtimes..."; \
	for agent in $$_agents; do \
		case "$$agent" in \
			claude-*) _adapter=claude_code ;; \
			codex-*)  _adapter=codex ;; \
			*)        _adapter=$(ADAPTER_KIND); \
				if [ -z "$$_adapter" ]; then \
					echo "WARNING: Cannot infer adapter for '$$agent' (no claude-*/codex-* prefix). Set ADAPTER_KIND explicitly."; \
					_adapter=codex; \
				fi ;; \
		esac; \
		case "$$agent" in \
			*-dev)      _caps="code,python" ;; \
			*-reviewer) _caps="review" ;; \
			*)          _caps="code" ;; \
		esac; \
		echo "  Starting rtm-$$agent (adapter=$$_adapter, caps=$$_caps)"; \
		nohup $(MAKE) runtime AGP_RUNTIME_ID="rtm-$$agent" AGP_RUNTIME_AGENT_ID="$$agent" \
			AGP_RUNTIME_CAPS="$$_caps" ADAPTER_KIND="$$_adapter" >/dev/null 2>&1 < /dev/null & \
	done; \
	sleep 5; \
	echo "Done. Check: make status"

restart: restart-runtime ## Restart CP + runtime workers from a clean runtime state

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
	if curl -sf --max-time 5 "$$_local/ops/health" >/dev/null 2>&1; then _url="$$_local"; _loc="LOCAL"; \
	elif curl -sf --max-time 5 "$$_remote/ops/health" >/dev/null 2>&1; then _url="$$_remote"; _loc="REMOTE"; fi; \
	\
	echo "=== Control Plane ==="; \
	if [ -n "$$_url" ]; then \
		echo "  location : $$_loc — $$_url"; \
		curl -s --max-time 5 "$$_url/ops/health" | python3 -c \
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
		curl -s --max-time 5 "$$_url/agents" | python3 -c \
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

.PHONY: runtime runtime-remote runtime-wezterm runtime-stop-remote runtime-stop-agent runtime-clean-tmux runtime-clean-wezterm runtime-clean-state runtime-fresh-agent runtime-clean runtime-deploy codex-dev codex-reviewer claude-dev claude-reviewer

runtime: ## Start a local runtime (agent self-registers with CP)
	$(call require_provider_env)
	@echo "Waiting for local control plane readiness..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		if curl -fsS --max-time 2 http://127.0.0.1:$(AGP_PORT)/health >/dev/null 2>&1; then \
			break; \
		fi; \
		if [ $$i -eq 10 ]; then \
			echo "ERROR: Local control plane is not healthy at http://127.0.0.1:$(AGP_PORT). Run make local-up first."; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@mkdir -p .agp-logs
	@echo "Starting runtime $(AGP_RUNTIME_ID) -> http://127.0.0.1:$(AGP_PORT) (agent=$(AGP_RUNTIME_AGENT_ID), adapter=$(ADAPTER_KIND), caps=$(AGP_RUNTIME_CAPS))"
	@echo "  Logs: .agp-logs/$(AGP_RUNTIME_ID).out"
	@echo "  (stdout/stderr redirected to log file — tail -f .agp-logs/$(AGP_RUNTIME_ID).out to monitor)"
	$(_PROVIDER_ENV) \
	AGP_ARTIFACT_BACKEND=http \
	$(if $(filter codex,$(ADAPTER_KIND)), \
		AGP_CODEX_TUI_MODE=true \
		AGP_CODEX_SESSION_MODE=sticky \
		AGP_CODEX_CLI_COMMAND="$(AGP_CODEX_CLI_COMMAND)" \
		AGP_CODEX_MAX_POLLS=240 \
		AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
		AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
		AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
		AGP_CODEX_IDLE_AFTER=5) \
	$(if $(filter claude_code,$(ADAPTER_KIND)), \
		AGP_CLAUDE_CODE_CLI_COMMAND="$(AGP_CLAUDE_CODE_CLI_COMMAND)" \
		AGP_CLAUDE_CODE_IDLE_POLL_SECONDS=2.0 \
		AGP_CLAUDE_CODE_IDLE_AFTER=3 \
		AGP_CLAUDE_CODE_IDLE_TIMEOUT_SECONDS=180.0 \
		AGP_CLAUDE_CODE_SESSION_MODE=sticky) \
	$(RUN) agp runtime-work-loop $(AGP_RUNTIME_ID) \
		--server-url http://127.0.0.1:$(AGP_PORT) \
		--host-kind tmux \
		--adapter-kind $(ADAPTER_KIND) \
		--agent-id $(AGP_RUNTIME_AGENT_ID) \
		--capabilities $(AGP_RUNTIME_CAPS) \
		$(if $(CWD),--workspace $(CWD)) \
	>> .agp-logs/$(AGP_RUNTIME_ID).out 2>&1

# ── Convenience runtime targets ──────────────────────────────────────

codex-dev: ## Start codex-dev agent (code,python capabilities)
	$(MAKE) runtime-fresh-agent AGP_RUNTIME_ID=rtm-codex-dev AGP_RUNTIME_AGENT_ID=codex-dev
	$(MAKE) runtime AGP_RUNTIME_ID=rtm-codex-dev AGP_RUNTIME_AGENT_ID=codex-dev AGP_RUNTIME_CAPS=code,python

codex-reviewer: ## Start codex-reviewer agent (review capability)
	$(MAKE) runtime-fresh-agent AGP_RUNTIME_ID=rtm-codex-reviewer AGP_RUNTIME_AGENT_ID=codex-reviewer
	$(MAKE) runtime AGP_RUNTIME_ID=rtm-codex-reviewer AGP_RUNTIME_AGENT_ID=codex-reviewer AGP_RUNTIME_CAPS=review

claude-dev: ## Start claude-dev agent (code,python capabilities)
	$(MAKE) runtime-fresh-agent AGP_RUNTIME_ID=rtm-claude-dev AGP_RUNTIME_AGENT_ID=claude-dev
	$(MAKE) runtime AGP_RUNTIME_ID=rtm-claude-dev AGP_RUNTIME_AGENT_ID=claude-dev AGP_RUNTIME_CAPS=code,python ADAPTER_KIND=claude_code

claude-reviewer: ## Start claude-reviewer agent (review capability)
	$(MAKE) runtime-fresh-agent AGP_RUNTIME_ID=rtm-claude-reviewer AGP_RUNTIME_AGENT_ID=claude-reviewer
	$(MAKE) runtime AGP_RUNTIME_ID=rtm-claude-reviewer AGP_RUNTIME_AGENT_ID=claude-reviewer AGP_RUNTIME_CAPS=review ADAPTER_KIND=claude_code

runtime-remote: ## Start a runtime connecting to remote CP
	$(call require_provider_env)
	$(call require_env,AGP_REMOTE_SERVER_URL)
	@echo "Starting runtime $(AGP_RUNTIME_ID) -> $(AGP_REMOTE_SERVER_URL) (agent=$(AGP_RUNTIME_AGENT_ID), caps=$(AGP_RUNTIME_CAPS))"
	AGP_SERVER_URL="$(AGP_REMOTE_SERVER_URL)" \
	AGP_ARTIFACT_BACKEND=http \
	AGP_RUNTIME_TERMINAL_HOST_KIND=tmux \
	AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
	AGP_CODEX_TUI_MODE=true \
	AGP_CODEX_SESSION_MODE=sticky \
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
	AGP_CODEX_SESSION_MODE=sticky \
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

runtime-stop-agent: ## Stop runtime worker process for AGP_RUNTIME_AGENT_ID
	@if [ -z "$(AGP_RUNTIME_AGENT_ID)" ]; then \
		echo "ERROR: AGP_RUNTIME_AGENT_ID is required"; \
		exit 1; \
	fi
	@pids="$$(ps -eo pid=,args= | awk '/[a]gp runtime-work-loop/ && index($$0, "--agent-id $(AGP_RUNTIME_AGENT_ID)") {print $$1}')"; \
	if [ -n "$$pids" ]; then \
		for pid in $$pids; do \
			$(SUDO) kill $$pid 2>/dev/null || kill $$pid 2>/dev/null || true; \
		done; \
	fi
	@echo "Runtime worker stopped for $(AGP_RUNTIME_AGENT_ID)."

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

runtime-clean-state: ## Delete local runtime artifacts, checkpoints, and temp caches
	@if command -v tmux >/dev/null 2>&1; then \
		for sess in $$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^agp-'); do \
			tmux kill-session -t "$$sess" 2>/dev/null || true; \
		done; \
	fi
	@if command -v wezterm >/dev/null 2>&1; then \
		wezterm cli list --format json 2>/dev/null | python3 -c 'import json, subprocess, sys; raw = sys.stdin.read().strip(); panes = json.loads(raw) if raw else []; [subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(p["pane_id"])], check=False) for p in panes if "AGP:" in (p.get("tab_title") or "")]' || true; \
	fi
	@uid="$$(id -u)"; \
	rm -rf \
		".agp-checkpoints" \
		"/tmp/agp-launches" \
		"/tmp/agp-tasks-$$uid" \
		"/tmp/agp-schemas-$$uid"; \
	rm -f .agp-logs/rtm-*.out; \
	mkdir -p .agp-checkpoints .agp-logs
	@echo "Runtime local state deleted."

runtime-fresh-agent: ## Fully reset one runtime agent before relaunch
	@if [ -z "$(AGP_RUNTIME_AGENT_ID)" ]; then \
		echo "ERROR: AGP_RUNTIME_AGENT_ID is required"; \
		exit 1; \
	fi
	@echo "Resetting runtime state for $(AGP_RUNTIME_AGENT_ID)..."
	@$(MAKE) runtime-stop-agent AGP_RUNTIME_AGENT_ID="$(AGP_RUNTIME_AGENT_ID)"
	@_url=""; \
	for u in http://127.0.0.1:$(AGP_PORT) $(AGP_REMOTE_SERVER_URL); do \
		[ -n "$$u" ] || continue; \
		if curl -sf --max-time 5 "$$u/ops/health" >/dev/null 2>&1 || curl -sf --max-time 5 "$$u/health" >/dev/null 2>&1; then _url="$$u"; break; fi; \
	done; \
	if [ -n "$$_url" ]; then \
		echo "Deregistering $(AGP_RUNTIME_AGENT_ID) from $$_url"; \
		$(RUN) agp down "$(AGP_RUNTIME_AGENT_ID)" --force --server-url "$$_url" >/dev/null 2>&1 || true; \
	else \
		echo "Control plane not reachable; skipping agent deregistration."; \
	fi
	@$(MAKE) runtime-clean-tmux AGP_RUNTIME_AGENT_ID="$(AGP_RUNTIME_AGENT_ID)"
	@$(MAKE) runtime-clean-wezterm AGP_RUNTIME_AGENT_ID="$(AGP_RUNTIME_AGENT_ID)"
	@rm -f \
		".agp-checkpoints/session-agp-$(AGP_RUNTIME_AGENT_ID).output.txt" \
		".agp-checkpoints/cursor-agp-$(AGP_RUNTIME_AGENT_ID).json" \
		".agp-logs/$(AGP_RUNTIME_ID).out"
	@echo "Fresh runtime state ready for $(AGP_RUNTIME_AGENT_ID)."

runtime-clean: ## Tear down ALL agents on the CP + kill local processes and sessions
	@echo "Tearing down all agents registered with CP..."
	@_url=""; \
	for u in http://127.0.0.1:$(AGP_PORT) $(AGP_REMOTE_SERVER_URL); do \
		if curl -sf --max-time 5 "$$u/ops/health" >/dev/null 2>&1; then _url="$$u"; break; fi; \
	done; \
	if [ -n "$$_url" ]; then \
		for agent_id in $$(curl -s --max-time 5 "$$_url/agents" | python3 -c "import sys,json; [print(a['agent_id']) for a in json.load(sys.stdin).get('data',{}).get('items',[])]" 2>/dev/null); do \
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
	@$(MAKE) runtime-clean-state
	@echo "Runtime cleanup complete."

runtime-deploy: ## Generate deploy script for a remote runtime
	@$(RUN) skyops runtime deploy rtm_remote --format script

# ── Orchestrator targets ─────────────────────────────────────────────

CODEX_ORC_SCRATCH := /tmp/codex-orc-scratch

codex-orc: ## Launch codex orchestrator (read-only sandbox, delegates to claude-dev)
	@mkdir -p $(CODEX_ORC_SCRATCH)
	codex \
		-a never \
		-s workspace-write \
		-c 'sandbox_workspace_write.network_access=true' \
		-C $(CODEX_ORC_SCRATCH) \
		"$$(cat $(ROOT)/docs/codex-orc-exploration-prompt.md)"

# ── Dev/test targets ─────────────────────────────────────────────────

.PHONY: test lint test-parser test-smallops test-live test-docker capture

test: ## Run test suite
	env -u SMALLOPS_LIVE -u SMALLOPS_DOCKER -u SMALLOPS_JUDGE $(RUN) python -m pytest tests/ smallops_tests/ -x -q --tb=short

test-parser: ## Run Claude Code parser tests only
	$(RUN) python -m pytest smallops_tests/ -m offline -v

test-smallops: ## Run smallops offline tests
	$(RUN) python -m pytest smallops_tests/ -m offline

test-live: ## Run smallops live tests (requires Claude Code)
	SMALLOPS_LIVE=1 $(RUN) python -m pytest smallops_tests/ -m live

test-docker: ## Run smallops Docker first-run tests
	# TODO: Replace this guard with `SMALLOPS_DOCKER=1 $(RUN) python -m pytest smallops_tests/ -m docker`
	# once Phase C first-run canaries exist.
	@echo "Phase C Docker first-run canaries are not implemented yet." >&2
	@echo "Add real tests under smallops_tests/ and replace this guard." >&2
	@exit 1

lint: ## Run linter
	$(RUN) python -m ruff check src/ tests/ smallops_tests/ scripts/inspect-smallops-corpus.py scripts/check-smallops-boundary.py
	$(RUN) python scripts/check-smallops-boundary.py
	$(RUN) lint-imports

capture: ## Capture pane to smallops corpus (usage: make capture CAT=ready NAME=fresh_launch [SESSION=agp-claude-reviewer])
	@test -n "$(CAT)" && test -n "$(NAME)" || (echo "Usage: make capture CAT=ready NAME=fresh_launch [SESSION=agp-claude-reviewer]" >&2; exit 1)
	@./scripts/capture-pane.sh "$(CAT)" "$(NAME)" "$(SESSION)" $(if $(FORCE),--force,)

# ── Help ─────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help
