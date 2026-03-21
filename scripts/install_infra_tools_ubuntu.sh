#!/usr/bin/env bash
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

sudo usermod -aG docker "${USER}" || true

echo "Installed:"
docker --version
docker compose version
kubectl version --client
echo
echo "If this is your first Docker install, start a new shell before using docker without sudo."
