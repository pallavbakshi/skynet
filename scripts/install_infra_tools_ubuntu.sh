#!/usr/bin/env bash
# DEPRECATED: Use `skyops deps install` instead.
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as your normal user, not root." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required" >&2
  exit 1
fi

sudo -n true >/dev/null 2>&1 || {
  echo "passwordless sudo is required for unattended setup on this host" >&2
  exit 1
}

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 curl ca-certificates
sudo systemctl daemon-reload
sudo systemctl enable --now docker

if ! command -v kubectl >/dev/null 2>&1; then
  sudo snap install kubectl --classic
fi

if ! command -v kind >/dev/null 2>&1; then
  ARCH="$(uname -m)"
  case "${ARCH}" in
    x86_64) KIND_ARCH="amd64" ;;
    aarch64|arm64) KIND_ARCH="arm64" ;;
    *)
      echo "Unsupported architecture for kind install: ${ARCH}" >&2
      exit 1
      ;;
  esac
  TMP_KIND="$(mktemp)"
  curl -fsSL "https://kind.sigs.k8s.io/dl/v0.29.0/kind-linux-${KIND_ARCH}" -o "${TMP_KIND}"
  chmod +x "${TMP_KIND}"
  sudo mv "${TMP_KIND}" /usr/local/bin/kind
fi

sudo usermod -aG docker "${USER}" || true

echo "Installed:"
docker --version
docker compose version
kubectl version --client
kind version
echo
echo "If this is your first Docker install, start a new shell before using docker without sudo."
