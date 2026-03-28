#!/usr/bin/env bash
# Install Docker Engine + Compose plugin — everything needed for `make up`.
# Supports: macOS, Ubuntu/Debian, Fedora/RHEL/Rocky, Arch
# Usage: bash scripts/install-docker.sh
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

# ── Docker Engine ─────────────────────────────────────────────────────
echo ""
echo "=== Docker Engine ==="

if command -v docker >/dev/null 2>&1; then
  ok "Docker already installed: $(docker --version)"
else
  case "${OS}" in
    macos)
      if command -v brew >/dev/null 2>&1; then
        echo "Installing Docker via Homebrew cask..."
        brew install --cask docker
        ok "Docker Desktop installed — open Docker.app to finish setup"
      else
        fail "Install Homebrew first (https://brew.sh) or download Docker Desktop from https://docker.com/products/docker-desktop"
      fi
      ;;
    debian)
      echo "Installing Docker via official apt repo..."
      sudo apt-get update -qq
      sudo apt-get install -y -qq ca-certificates curl gnupg
      sudo install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg | \
        sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
      sudo chmod a+r /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
      sudo apt-get update -qq
      sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ok "Docker installed"
      ;;
    fedora)
      echo "Installing Docker via official dnf repo..."
      sudo dnf -y install dnf-plugins-core
      sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo 2>/dev/null || \
        sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
      sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ok "Docker installed"
      ;;
    arch)
      sudo pacman -Sy --noconfirm docker docker-compose docker-buildx
      ok "Docker installed"
      ;;
  esac
fi

# ── Start Docker daemon (Linux) ──────────────────────────────────────
if [[ "${OS}" != "macos" ]]; then
  echo ""
  echo "=== Starting Docker daemon ==="
  sudo systemctl enable --now docker 2>/dev/null || true

  # Add current user to docker group so sudo isn't needed
  if ! groups | grep -qw docker; then
    sudo usermod -aG docker "${USER}" 2>/dev/null || true
    warn "Added ${USER} to docker group — log out and back in for it to take effect"
  fi
fi

# ── Docker Compose plugin ────────────────────────────────────────────
echo ""
echo "=== Docker Compose ==="

if docker compose version >/dev/null 2>&1; then
  ok "Docker Compose: $(docker compose version --short 2>/dev/null || docker compose version)"
else
  case "${OS}" in
    macos)
      warn "Docker Compose comes with Docker Desktop — open Docker.app first"
      ;;
    *)
      # Compose plugin should have been installed above; try standalone as fallback
      COMPOSE_VERSION="v2.32.4"
      COMPOSE_ARCH="$(uname -m)"
      case "${COMPOSE_ARCH}" in
        x86_64)  COMPOSE_ARCH="x86_64" ;;
        aarch64) COMPOSE_ARCH="aarch64" ;;
      esac
      COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}"
      echo "Installing Docker Compose ${COMPOSE_VERSION}..."
      sudo curl -fsSL "${COMPOSE_URL}" -o /usr/local/lib/docker/cli-plugins/docker-compose 2>/dev/null || \
        (sudo mkdir -p /usr/local/lib/docker/cli-plugins && \
         sudo curl -fsSL "${COMPOSE_URL}" -o /usr/local/lib/docker/cli-plugins/docker-compose)
      sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
      ok "Docker Compose installed"
      ;;
  esac
fi

# ── Verify ────────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="

docker --version >/dev/null 2>&1       && ok "docker CLI"       || warn "docker not in PATH"
docker compose version >/dev/null 2>&1 && ok "docker compose"   || warn "docker compose not available"

# Check daemon is running (skip on macOS if Docker Desktop hasn't started)
if docker info >/dev/null 2>&1; then
  ok "Docker daemon running"
else
  if [[ "${OS}" == "macos" ]]; then
    warn "Docker daemon not running — open Docker Desktop to start it"
  else
    warn "Docker daemon not running — try: sudo systemctl start docker"
  fi
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Docker install complete."
echo ""
echo " Next steps:"
echo ""
echo "   make up     # start CP + postgres + redis + minio + monitoring"
echo "   make ps     # check container status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
