#!/usr/bin/env bash
# Install AGP runtime on any Linux box (minimal — no postgres/redis/minio).
# This is for machines that ONLY run a runtime worker, not the control plane.
# Usage: bash scripts/install-runtime.sh
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── OS detection ──────────────────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
  fail "Use install-mac.sh for macOS."
fi

if [[ ! -f /etc/os-release ]]; then
  fail "Cannot detect OS. Needs /etc/os-release."
fi

. /etc/os-release
echo "Detected: ${PRETTY_NAME:-${ID} ${VERSION_ID}}"

# ── Python ────────────────────────────────────────────────────────────
echo ""
echo "=== Python ==="

install_python() {
  case "${ID}" in
    ubuntu|debian)
      sudo apt-get update -qq
      sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip || \
        sudo apt-get install -y -qq python3 python3-venv python3-pip
      ;;
    fedora|rhel|centos|rocky|alma)
      sudo dnf install -y python3.12 python3-pip || \
        sudo dnf install -y python3 python3-pip
      ;;
    arch|manjaro)
      sudo pacman -Sy --noconfirm python python-pip
      ;;
    *)
      fail "Unsupported distro: ${ID}. Install Python 3.12+ manually."
      ;;
  esac
}

if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if python3 -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)"; then
    ok "Python ${PY_VER}"
  else
    warn "Python ${PY_VER} too old, installing 3.12..."
    install_python
  fi
else
  install_python
fi

# ── tmux ──────────────────────────────────────────────────────────────
echo ""
echo "=== tmux ==="

if command -v tmux >/dev/null 2>&1; then
  ok "tmux installed: $(tmux -V)"
else
  case "${ID}" in
    ubuntu|debian)  sudo apt-get install -y -qq tmux ;;
    fedora|rhel|centos|rocky|alma) sudo dnf install -y tmux ;;
    arch|manjaro)   sudo pacman -Sy --noconfirm tmux ;;
    *)              warn "Install tmux manually" ;;
  esac
  command -v tmux >/dev/null 2>&1 && ok "tmux installed" || warn "tmux not installed"
fi

# ── uv ────────────────────────────────────────────────────────────────
echo ""
echo "=== Python tooling ==="

if command -v uv >/dev/null 2>&1; then
  ok "uv installed: $(uv --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed"
fi

# ── Install AGP (SDK only) ───────────────────────────────────────────
echo ""
echo "=== Installing AGP ==="

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd 2>/dev/null || echo ".")"
cd "${ROOT}"

if [[ -f pyproject.toml ]]; then
  uv pip install -e "." 2>/dev/null || pip install -e "."
  ok "AGP installed from source (SDK)"
else
  pip install agp
  ok "AGP installed from PyPI (SDK)"
fi

# ── Codex (optional) ─────────────────────────────────────────────────
echo ""
echo "=== Codex CLI (optional) ==="

if command -v codex >/dev/null 2>&1; then
  ok "codex installed"
elif command -v npm >/dev/null 2>&1; then
  npm install -g @openai/codex 2>/dev/null && ok "codex installed" || warn "codex install failed"
else
  warn "npm not found — install Node.js + codex manually if using codex adapter"
fi

# ── Verify ────────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="

python3 -c "from agp.client import AgpClient; print('agp.client OK')" 2>/dev/null && ok "agp SDK importable" || warn "agp SDK import failed"
command -v agp >/dev/null 2>&1 && ok "agp CLI in PATH" || warn "agp CLI not in PATH — check your PATH"
command -v tmux >/dev/null 2>&1 && ok "tmux in PATH" || warn "tmux not in PATH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Runtime install complete."
echo ""
echo " To join a control plane:"
echo ""
echo "   export OPENAI_API_KEY='sk-...'   # for codex adapter"
echo ""
echo "   AGP_ARTIFACT_BACKEND=http agp runtime-work-loop rtm_worker \\"
echo "     --server-url http://<cp-host>:7860 \\"
echo "     --host-kind tmux \\"
echo "     --adapter-kind codex \\"
echo "     --agent-id agt_worker"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
