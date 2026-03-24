#!/usr/bin/env bash
# Install AGP control plane + runtime (orc) on this machine.
# Installs: Python 3.12, PostgreSQL 16, Redis 7, MinIO, tmux, Node.js, codex, uv, agp[server]
# Supports: macOS, Ubuntu/Debian, Fedora/RHEL/Rocky, Arch
# Usage: bash scripts/install-server.sh
set -euo pipefail

# ── Versions ──────────────────────────────────────────────────────────
PYTHON_MIN="3.12"
POSTGRES_VERSION="16"
MINIO_VERSION="RELEASE.2025-02-28T09-55-16Z"

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

# pip (non-mac)
if [[ "${OS}" != "macos" ]] && ! command -v pip3 >/dev/null 2>&1; then
  pkg_install python3-pip 2>/dev/null || true
fi

# ── PostgreSQL ────────────────────────────────────────────────────────
echo ""
echo "=== PostgreSQL ==="

if command -v psql >/dev/null 2>&1; then
  ok "PostgreSQL already installed: $(psql --version | head -1)"
else
  case "${OS}" in
    macos)  brew install postgresql@${POSTGRES_VERSION} && brew services start postgresql@${POSTGRES_VERSION} ;;
    debian) pkg_install "postgresql-${POSTGRES_VERSION}" "postgresql-client-${POSTGRES_VERSION}" || pkg_install postgresql postgresql-client ;;
    fedora) pkg_install postgresql-server postgresql && sudo postgresql-setup --initdb 2>/dev/null || true ;;
    arch)   pkg_install postgresql && sudo -u postgres initdb -D /var/lib/postgres/data 2>/dev/null || true ;;
  esac
  ok "PostgreSQL installed"
fi

# Start postgres
case "${OS}" in
  macos)  brew services start postgresql@${POSTGRES_VERSION} 2>/dev/null || true ;;
  *)      sudo systemctl enable --now postgresql 2>/dev/null || true ;;
esac

# Wait for postgres
for _ in {1..10}; do
  if [[ "${OS}" == "macos" ]]; then
    pg_isready -q 2>/dev/null && break
  else
    sudo -u postgres pg_isready -q 2>/dev/null && break
  fi
  sleep 1
done

# Create agp user and database
if [[ "${OS}" == "macos" ]]; then
  createuser agp 2>/dev/null || true
  createdb -O agp agp 2>/dev/null || true
  psql -d agp -c "ALTER USER agp PASSWORD 'agp';" 2>/dev/null || true
else
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='agp'" 2>/dev/null | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER agp WITH PASSWORD 'agp';" 2>/dev/null
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='agp'" 2>/dev/null | grep -q 1 || \
    sudo -u postgres createdb -O agp agp
  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE agp TO agp;" 2>/dev/null || true
fi
ok "Database 'agp' ready"

# ── Redis ─────────────────────────────────────────────────────────────
echo ""
echo "=== Redis ==="

if command -v redis-server >/dev/null 2>&1; then
  ok "Redis already installed"
else
  case "${OS}" in
    macos)  brew install redis ;;
    debian) pkg_install redis-server ;;
    fedora) pkg_install redis ;;
    arch)   pkg_install redis ;;
  esac
  ok "Redis installed"
fi

case "${OS}" in
  macos)  brew services start redis 2>/dev/null || true ;;
  *)      sudo systemctl enable --now redis-server 2>/dev/null || sudo systemctl enable --now redis 2>/dev/null || true ;;
esac

redis-cli ping >/dev/null 2>&1 && ok "Redis running" || warn "Redis not responding"

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

if command -v ncodex >/dev/null 2>&1; then
  ok "ncodex already installed"
elif command -v codex >/dev/null 2>&1; then
  ok "codex already installed"
elif command -v claude >/dev/null 2>&1; then
  ok "claude already installed"
else
  npm install -g @openai/codex 2>/dev/null && ok "codex installed" || \
    sudo npm install -g @openai/codex 2>/dev/null && ok "codex installed" || \
    warn "codex install failed — install manually: npm install -g @openai/codex"
fi

# ── MinIO ─────────────────────────────────────────────────────────────
echo ""
echo "=== MinIO (optional — S3 artifact backend) ==="

if command -v minio >/dev/null 2>&1; then
  ok "MinIO already installed"
else
  case "${OS}" in
    macos)
      brew install minio/stable/minio && ok "MinIO installed" || warn "MinIO install failed"
      ;;
    debian|fedora|arch)
      ARCH="$(uname -m)"
      case "${ARCH}" in
        x86_64)  MINIO_ARCH="amd64" ;;
        aarch64) MINIO_ARCH="arm64" ;;
        *)       MINIO_ARCH="${ARCH}" ;;
      esac
      MINIO_URL="https://dl.min.io/server/minio/release/linux-${MINIO_ARCH}/archive/minio.${MINIO_VERSION}"
      echo "Downloading MinIO ${MINIO_VERSION} (${MINIO_ARCH})..."
      sudo curl -fsSL "${MINIO_URL}" -o /usr/local/bin/minio && sudo chmod +x /usr/local/bin/minio && ok "MinIO installed" || warn "MinIO download failed"
      ;;
  esac
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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -f pyproject.toml ]]; then
  if command -v uv >/dev/null 2>&1; then
    # uv sync creates/updates .venv and installs the project + extras.
    # Commands are then available via `uv run agp`, `uv run skyops`.
    uv sync --extra server 2>/dev/null || uv pip install -e ".[server]"
    ok "AGP installed (use 'uv run agp' or 'make' targets)"
  else
    pip install -e ".[server]"
    ok "AGP installed from source with [server] extras"
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

# Check direct PATH first, then uv run
(command -v agp >/dev/null 2>&1 && agp --help >/dev/null 2>&1) || \
  (command -v uv >/dev/null 2>&1 && uv run agp --help >/dev/null 2>&1) && ok "agp CLI" || warn "agp not available"
(command -v skyops >/dev/null 2>&1 && skyops --help >/dev/null 2>&1) || \
  (command -v uv >/dev/null 2>&1 && uv run skyops --help >/dev/null 2>&1) && ok "skyops CLI" || warn "skyops not available"
tmux -V >/dev/null 2>&1         && ok "tmux"             || warn "tmux not in PATH"
(command -v ncodex >/dev/null 2>&1 && ok "ncodex") || \
  (command -v codex >/dev/null 2>&1 && ok "codex (ncodex not found)") || \
  warn "codex/ncodex not in PATH"
redis-cli ping >/dev/null 2>&1  && ok "redis reachable"  || warn "redis not reachable"
if [[ "${OS}" == "macos" ]]; then
  pg_isready -q 2>/dev/null     && ok "postgres reachable" || warn "postgres not reachable"
else
  sudo -u postgres pg_isready -q 2>/dev/null && ok "postgres reachable" || warn "postgres not reachable"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Server install complete."
echo ""
echo " Next steps:"
echo ""
echo "   make initdb    # create database schema"
echo "   make serve     # start CP + sweepers"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
