#!/usr/bin/env bash
# Install AGP runtime (co-agent) on this machine.
# Installs: Python 3.12, tmux, Node.js, codex, uv, agp SDK
# NO infra (no postgres, redis, minio) — connects to a remote CP.
# Supports: macOS, Ubuntu/Debian, Fedora/RHEL/Rocky, Arch
# Usage: bash scripts/install-runtime.sh
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── OS detection ──────────────────────────────────────────────────────
OS=""
if [[ "$(uname)" == "Darwin" ]]; then
  OS="macos"
elif [[ -f /etc/os-release ]]; then
  . /etc/os-release
  case "${ID}" in
    ubuntu|debian)           OS="debian" ;;
    fedora|rhel|centos|rocky|alma) OS="fedora" ;;
    arch|manjaro)            OS="arch" ;;
    *)                       OS="debian" ; warn "Unknown distro '${ID}', trying Debian commands" ;;
  esac
else
  fail "Cannot detect OS."
fi
echo "Detected: ${OS}"

# ── Package install helpers ───────────────────────────────────────────
pkg_install() {
  case "${OS}" in
    macos)  brew install "$@" ;;
    debian) sudo apt-get install -y -qq "$@" ;;
    fedora) sudo dnf install -y "$@" ;;
    arch)   sudo pacman -Sy --noconfirm "$@" ;;
  esac
}

pkg_update() {
  case "${OS}" in
    macos)  brew update ;;
    debian) sudo apt-get update -qq ;;
    fedora) sudo dnf check-update -q || true ;;
    arch)   sudo pacman -Sy ;;
  esac
}

# ── Homebrew (macOS only) ─────────────────────────────────────────────
if [[ "${OS}" == "macos" ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  ok "Homebrew"
fi

echo ""
echo "=== Updating package index ==="
pkg_update

# ── Python 3.12+ ─────────────────────────────────────────────────────
echo ""
echo "=== Python ==="

need_python=true
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if python3 -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)"; then
    ok "Python ${PY_VER}"
    need_python=false
  else
    warn "Python ${PY_VER} too old"
  fi
fi

if $need_python; then
  case "${OS}" in
    macos)  brew install python@3.12 ;;
    debian) pkg_install python3.12 python3.12-venv python3.12-dev || pkg_install python3 python3-venv python3-dev ;;
    fedora) pkg_install python3.12 python3.12-devel || pkg_install python3 python3-devel ;;
    arch)   pkg_install python ;;
  esac
  ok "Python installed"
fi

if [[ "${OS}" != "macos" ]] && ! command -v pip3 >/dev/null 2>&1; then
  pkg_install python3-pip 2>/dev/null || true
fi

# ── tmux ──────────────────────────────────────────────────────────────
echo ""
echo "=== tmux ==="

if command -v tmux >/dev/null 2>&1; then
  ok "tmux already installed: $(tmux -V)"
else
  pkg_install tmux
  ok "tmux installed"
fi

# ── Node.js + Codex ──────────────────────────────────────────────────
echo ""
echo "=== Node.js + Codex ==="

if command -v node >/dev/null 2>&1; then
  ok "Node.js already installed: $(node --version)"
else
  case "${OS}" in
    macos)  brew install node ;;
    debian) curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - 2>/dev/null; pkg_install nodejs ;;
    fedora) pkg_install nodejs ;;
    arch)   pkg_install nodejs npm ;;
  esac
  ok "Node.js installed"
fi

if command -v codex >/dev/null 2>&1; then
  ok "codex already installed"
else
  npm install -g @openai/codex 2>/dev/null && ok "codex installed" || \
    sudo npm install -g @openai/codex 2>/dev/null && ok "codex installed" || \
    warn "codex install failed — install manually: npm install -g @openai/codex"
fi

# ── uv ────────────────────────────────────────────────────────────────
echo ""
echo "=== uv (Python package manager) ==="

if command -v uv >/dev/null 2>&1; then
  ok "uv already installed: $(uv --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed"
fi

# ── Install AGP ──────────────────────────────────────────────────────
echo ""
echo "=== Installing AGP ==="

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd 2>/dev/null || echo ".")"
cd "${ROOT}"

if [[ -f pyproject.toml ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv sync --extra server 2>/dev/null || uv pip install -e ".[server]"
    ok "AGP installed (use 'uv run agp' or 'make' targets)"
  else
    pip install -e ".[server]"
    ok "AGP installed from source"
  fi
else
  if command -v uv >/dev/null 2>&1; then
    uv pip install "agp[server]"
  else
    pip install "agp[server]"
  fi
  ok "AGP installed from PyPI"
fi

# ── Verify ────────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="

(command -v agp >/dev/null 2>&1 && agp --help >/dev/null 2>&1) || \
  (command -v uv >/dev/null 2>&1 && uv run agp --help >/dev/null 2>&1) && ok "agp CLI" || warn "agp not available"
(python3 -c "from agp.client import AgpClient" 2>/dev/null) || \
  (command -v uv >/dev/null 2>&1 && uv run python -c "from agp.client import AgpClient" 2>/dev/null) && ok "agp SDK" || warn "agp SDK import failed"
tmux -V >/dev/null 2>&1                 && ok "tmux"      || warn "tmux not in PATH"
command -v codex >/dev/null 2>&1         && ok "codex"    || warn "codex not in PATH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Runtime install complete."
echo ""
echo " To join a control plane:"
echo ""
echo "   export OPENAI_API_KEY='sk-...'   # for codex adapter"
echo ""
echo "   AGP_ARTIFACT_BACKEND=http agp runtime-work-loop rtm_worker \\"
echo "     --server-url http://<server-ip>:7860 \\"
echo "     --host-kind tmux \\"
echo "     --adapter-kind codex \\"
echo "     --agent-id agt_worker"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
