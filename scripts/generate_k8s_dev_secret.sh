#!/usr/bin/env bash
# DEPRECATED: Use `skyops secrets generate-k8s` (future) instead.
set -euo pipefail

OUT_PATH="${1:-/tmp/agp-k8s-secret.dev.yaml}"

DB_URL="${AGP_DATABASE_URL:-postgresql+psycopg://agp:agp@postgres:5432/agp}"
S3_ACCESS_KEY_ID="${AGP_S3_ACCESS_KEY_ID:-minioadmin}"
S3_SECRET_ACCESS_KEY="${AGP_S3_SECRET_ACCESS_KEY:-minioadmin}"
OPERATOR_TOKEN_ROLES_JSON="${AGP_OPERATOR_TOKEN_ROLES_JSON:-{}}"
RUNTIME_ACTIVE_TOKENS_JSON="${AGP_RUNTIME_ACTIVE_TOKENS_JSON:-[]}"
ALERT_WEBHOOK_URL="${AGP_OBSERVABILITY_ALERT_WEBHOOK_URL:-}"

cat > "${OUT_PATH}" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: agp-secrets
  namespace: agp
type: Opaque
stringData:
  AGP_DATABASE_URL: "${DB_URL}"
  AGP_S3_ACCESS_KEY_ID: "${S3_ACCESS_KEY_ID}"
  AGP_S3_SECRET_ACCESS_KEY: "${S3_SECRET_ACCESS_KEY}"
  AGP_OPERATOR_TOKEN_ROLES_JSON: '${OPERATOR_TOKEN_ROLES_JSON}'
  AGP_RUNTIME_ACTIVE_TOKENS_JSON: '${RUNTIME_ACTIVE_TOKENS_JSON}'
  AGP_OBSERVABILITY_ALERT_WEBHOOK_URL: "${ALERT_WEBHOOK_URL}"
EOF

echo "${OUT_PATH}"
