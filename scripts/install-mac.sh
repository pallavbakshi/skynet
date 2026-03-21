#!/usr/bin/env bash
# Install AGP runtime + SDK on macOS (for running a co-agent).
# Usage: bash scripts/install-mac.sh
set -euo pipefail

# ── Versions ──────────────────────────────────────────────────────────
PYTHON_MIN="3.12"

# ── Colors ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── OS check ──────────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
  fail "This script is for macOS. Use install-server.sh for Ubuntu/Debian."
fi

# ── Homebrew ──────────────────────────────────────────────────────────
echo ""
echo "=== Checking Homebrew ==="

if command -v brew >/dev/null 2>&1; then
  ok "Homebrew installed"
else
  echo "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ok "Homebrew installed"
fi

# ── Python ────────────────────────────────────────────────────────────
echo ""
echo "=== Python ==="

if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if python3 -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)"; then
    ok "Python ${PY_VER}"
  else
    warn "Python ${PY_VER} too old, installing 3.12..."
    brew install python@3.12
  fi
else
  brew install python@3.12
fi

# ── tmux (for terminal host) ─────────────────────────────────────────
echo ""
echo "=== tmux ==="

if command -v tmux >/dev/null 2>&1; then
  ok "tmux installed: $(tmux -V)"
else
  brew install tmux
  ok "tmux installed"
fi

# ── uv (Python package manager) ──────────────────────────────────────
echo ""
echo "=== Python tooling ==="

if command -v uv >/dev/null 2>&1; then
  ok "uv installed: $(uv --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed"
fi

# ── Install AGP (SDK only — no [server] extras needed) ───────────────
echo ""
echo "=== Installing AGP ==="

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -f pyproject.toml ]]; then
  uv pip install -e ".[server]" 2>/dev/null || pip install -e ".[server]"
  ok "AGP installed from source"
else
  pip install agp
  ok "AGP installed from PyPI (SDK only)"
fi

# ── Codex (optional — for codex adapter) ─────────────────────────────
echo ""
echo "=== Codex CLI (optional) ==="

if command -v codex >/dev/null 2>&1; then
  ok "codex installed"
else
  if command -v npm >/dev/null 2>&1; then
    npm install -g @openai/codex 2>/dev/null && ok "codex installed via npm" || warn "codex install failed — install manually: npm install -g @openai/codex"
  else
    warn "npm not found — install codex manually: npm install -g @openai/codex"
  fi
fi

# ── Verify ────────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="

agp --help >/dev/null 2>&1 && ok "agp CLI works" || warn "agp CLI not in PATH"
skyops --help >/dev/null 2>&1 && ok "skyops CLI works" || warn "skyops CLI not in PATH"
tmux -V >/dev/null 2>&1 && ok "tmux works" || warn "tmux not in PATH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Mac install complete."
echo ""
echo " To connect to a remote control plane:"
echo ""
echo "   export OPENAI_API_KEY='sk-...'   # for codex adapter"
echo ""
echo "   AGP_ARTIFACT_BACKEND=http agp runtime-work-loop rtm_mac \\"
echo "     --server-url http://<server-ip>:7860 \\"
echo "     --host-kind tmux \\"
echo "     --adapter-kind codex \\"
echo "     --agent-id agt_mac"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
