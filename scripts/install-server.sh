#!/usr/bin/env bash
# Install AGP control plane + infrastructure on a server (Ubuntu/Debian).
# Usage: bash scripts/install-server.sh
set -euo pipefail

# ── Versions ──────────────────────────────────────────────────────────
PYTHON_MIN="3.12"
POSTGRES_VERSION="16"
REDIS_VERSION="7"
MINIO_VERSION="RELEASE.2025-02-28T09-55-16Z"

# ── Colors ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── OS detection ──────────────────────────────────────────────────────
detect_os() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_ID="${ID}"
    OS_VERSION="${VERSION_ID}"
  elif [[ "$(uname)" == "Darwin" ]]; then
    OS_ID="macos"
    OS_VERSION="$(sw_vers -productVersion)"
  else
    fail "Unsupported OS"
  fi
}

detect_os

if [[ "${OS_ID}" != "ubuntu" && "${OS_ID}" != "debian" ]]; then
  fail "This script targets Ubuntu/Debian. Got: ${OS_ID}. Use install-mac.sh for macOS."
fi

# ── System packages ───────────────────────────────────────────────────
echo ""
echo "=== Installing system packages ==="

sudo apt-get update -qq

# Python
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if python3 -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)"; then
    ok "Python ${PY_VER} (>= ${PYTHON_MIN})"
  else
    warn "Python ${PY_VER} found but need >= ${PYTHON_MIN}"
    sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
  fi
else
  sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
fi

# pip / venv
sudo apt-get install -y -qq python3-pip python3-venv

# PostgreSQL
if ! command -v psql >/dev/null 2>&1; then
  echo "Installing PostgreSQL ${POSTGRES_VERSION}..."
  sudo apt-get install -y -qq "postgresql-${POSTGRES_VERSION}" "postgresql-client-${POSTGRES_VERSION}" || \
    sudo apt-get install -y -qq postgresql postgresql-client
  ok "PostgreSQL installed"
else
  ok "PostgreSQL already installed: $(psql --version | head -1)"
fi

# Redis
if ! command -v redis-server >/dev/null 2>&1; then
  echo "Installing Redis..."
  sudo apt-get install -y -qq redis-server
  ok "Redis installed"
else
  ok "Redis already installed: $(redis-server --version | head -1)"
fi

# ── Start services ────────────────────────────────────────────────────
echo ""
echo "=== Starting services ==="

sudo systemctl enable --now postgresql 2>/dev/null || true
sudo systemctl enable --now redis-server 2>/dev/null || true

# Wait for postgres
for _ in {1..10}; do
  if sudo -u postgres pg_isready -q 2>/dev/null; then break; fi
  sleep 1
done
sudo -u postgres pg_isready -q && ok "PostgreSQL running" || fail "PostgreSQL not ready"

redis-cli ping >/dev/null 2>&1 && ok "Redis running" || fail "Redis not ready"

# ── Create AGP database ──────────────────────────────────────────────
echo ""
echo "=== Setting up AGP database ==="

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='agp'" | grep -q 1; then
  ok "User 'agp' exists"
else
  sudo -u postgres psql -c "CREATE USER agp WITH PASSWORD 'agp';"
  ok "Created user 'agp'"
fi

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='agp'" | grep -q 1; then
  ok "Database 'agp' exists"
else
  sudo -u postgres createdb -O agp agp
  ok "Created database 'agp'"
fi

# Grant connect (idempotent)
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE agp TO agp;" 2>/dev/null || true

# ── Install MinIO (optional, for S3 artifact backend) ────────────────
echo ""
echo "=== MinIO (optional — for S3 artifact backend) ==="

if command -v minio >/dev/null 2>&1; then
  ok "MinIO already installed"
else
  ARCH="$(dpkg --print-architecture)"
  MINIO_URL="https://dl.min.io/server/minio/release/linux-${ARCH}/archive/minio.${MINIO_VERSION}"
  echo "Downloading MinIO ${MINIO_VERSION} (${ARCH})..."
  sudo curl -fsSL "${MINIO_URL}" -o /usr/local/bin/minio
  sudo chmod +x /usr/local/bin/minio
  ok "MinIO ${MINIO_VERSION} installed"
fi

# ── Install uv (Python package manager) ──────────────────────────────
echo ""
echo "=== Python tooling ==="

if command -v uv >/dev/null 2>&1; then
  ok "uv already installed: $(uv --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed: $(uv --version)"
fi

# ── Install AGP ──────────────────────────────────────────────────────
echo ""
echo "=== Installing AGP ==="

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -f pyproject.toml ]]; then
  uv pip install -e ".[server]" 2>/dev/null || pip install -e ".[server]"
  ok "AGP installed from source with [server] extras"
else
  pip install "agp[server]"
  ok "AGP installed from PyPI"
fi

# ── Verify ────────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="

agp --help >/dev/null 2>&1 && ok "agp CLI works" || warn "agp CLI not in PATH"
skyops --help >/dev/null 2>&1 && ok "skyops CLI works" || warn "skyops CLI not in PATH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Server install complete."
echo ""
echo " Next: start the control plane:"
echo ""
echo "   export AGP_HOST=0.0.0.0"
echo "   export AGP_PORT=7860"
echo "   export AGP_DATABASE_URL='postgresql+psycopg://agp:agp@localhost:5432/agp'"
echo "   export AGP_QUEUE_BACKEND=redis"
echo "   export AGP_REDIS_URL='redis://localhost:6379/0'"
echo "   export AGP_ARTIFACT_BACKEND=localfs"
echo "   export AGP_LOG_ROOT=/tmp/agp-logs"
echo ""
echo "   agp initdb"
echo "   agp serve"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
