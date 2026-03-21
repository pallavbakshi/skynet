#!/usr/bin/env bash
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required on macOS." >&2
  exit 1
fi

brew install kubectl kind
brew install --cask docker

echo "Installed:"
kubectl version --client
kind version
echo
echo "Docker Desktop was installed via Homebrew."
echo "Start Docker Desktop before using the Phase 3 stack scripts."
