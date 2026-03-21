#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-agp-phase3}"
KIND_CONFIG_PATH="${ROOT}/.kind-agp-phase3.yaml"
KUSTOMIZE_PATH="${KUSTOMIZE_PATH:-k8s/overlays/kind}"
PORT_FORWARD_PORT="${PORT_FORWARD_PORT:-17860}"
SMOKE_KUBECONFIG="${ROOT}/.kubeconfig-kind-${KIND_CLUSTER_NAME}"
RECREATE_CLUSTER="${RECREATE_CLUSTER:-true}"
SKIP_IMAGE_BUILD="${SKIP_IMAGE_BUILD:-false}"
SKIP_IMAGE_LOAD="${SKIP_IMAGE_LOAD:-false}"
KIND_NODE_CONTAINER="${KIND_CLUSTER_NAME}-control-plane"
TMP_OVERLAY_DIR="$(mktemp -d)"
TMP_SECRET_PATCH="${TMP_OVERLAY_DIR}/patch-secret.yaml"
TMP_KUSTOMIZATION="${TMP_OVERLAY_DIR}/kustomization.yaml"
TMP_BASE_LINK="${TMP_OVERLAY_DIR}/base"
TMP_RENDERED_MANIFEST="${TMP_OVERLAY_DIR}/rendered.yaml"
NODE_RENDERED_MANIFEST="/tmp/rendered.yaml"
TMP_SMOKE_JOB_MANIFEST="${TMP_OVERLAY_DIR}/smoke-job.yaml"
NODE_SMOKE_JOB_MANIFEST="/tmp/agp-k8s-smoke-job.yaml"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
  KIND_MODE="direct"
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
  if user docker -c 'docker info >/dev/null 2>&1'; then
    KIND_MODE="user"
  else
    KIND_MODE="sudo"
  fi
else
  echo "Docker daemon is not reachable" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi
if ! command -v kind >/dev/null 2>&1; then
  echo "kind is required" >&2
  exit 1
fi

kind_run() {
  case "${KIND_MODE}" in
    direct)
      kind "$@"
      ;;
    user)
      user docker -c "KUBECONFIG='${KUBECONFIG:-$HOME/.kube/config}' PATH='${PATH}' kind $*"
      ;;
    sudo)
      sudo -n env "KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config}" "PATH=${PATH}" kind "$@"
      ;;
    *)
      echo "unsupported KIND_MODE=${KIND_MODE}" >&2
      exit 1
      ;;
  esac
}

if command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run python)
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_CMD=(.venv/bin/python)
else
  PYTHON_CMD=(python)
fi

cat > "${KIND_CONFIG_PATH}" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
EOF

if kind_run get clusters | grep -qx "${KIND_CLUSTER_NAME}"; then
  if [[ "${RECREATE_CLUSTER}" == "true" ]]; then
    kind_run delete cluster --name "${KIND_CLUSTER_NAME}"
  fi
fi

if ! kind_run get clusters | grep -qx "${KIND_CLUSTER_NAME}"; then
  kind_run create cluster --name "${KIND_CLUSTER_NAME}" --config "${KIND_CONFIG_PATH}"
fi
kind_run export kubeconfig --name "${KIND_CLUSTER_NAME}" --kubeconfig "${SMOKE_KUBECONFIG}"
if [[ -f "${SMOKE_KUBECONFIG}" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    sudo -n chown "$(id -u):$(id -g)" "${SMOKE_KUBECONFIG}" 2>/dev/null || true
  fi
  chmod 600 "${SMOKE_KUBECONFIG}" 2>/dev/null || true
fi

if [[ "${SKIP_IMAGE_BUILD}" != "true" ]]; then
  "${DOCKER[@]}" build -t agp:latest .
fi
if [[ "${SKIP_IMAGE_LOAD}" != "true" ]]; then
  kind_run load docker-image agp:latest --name "${KIND_CLUSTER_NAME}"
fi

./scripts/generate_k8s_dev_secret.sh "${TMP_SECRET_PATCH}" >/dev/null
ln -s "${ROOT}/${KUSTOMIZE_PATH}" "${TMP_BASE_LINK}"
cat > "${TMP_KUSTOMIZATION}" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./base
patches:
  - path: ./patch-secret.yaml
EOF

cleanup() {
  rm -rf "${TMP_OVERLAY_DIR}" "${KIND_CONFIG_PATH}" "${SMOKE_KUBECONFIG}"
}
trap cleanup EXIT

KCTL=("${DOCKER[@]}" exec "${KIND_NODE_CONTAINER}" kubectl)

"${KCTL[@]}" delete namespace agp --ignore-not-found --wait=true
KUBECONFIG="${SMOKE_KUBECONFIG}" kubectl kustomize "${TMP_OVERLAY_DIR}" --load-restrictor=LoadRestrictionsNone > "${TMP_RENDERED_MANIFEST}"
"${DOCKER[@]}" exec -i "${KIND_NODE_CONTAINER}" sh -lc "cat > '${NODE_RENDERED_MANIFEST}'" < "${TMP_RENDERED_MANIFEST}"
"${KCTL[@]}" apply -f "${NODE_RENDERED_MANIFEST}"

"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/postgres --timeout=180s
"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/minio --timeout=180s
"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/redis --timeout=180s
"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/control-plane --timeout=180s
for _ in $(seq 1 180); do
  bootstrap_succeeded="$("${KCTL[@]}" get job agp-bootstrap --namespace agp -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  if [[ "${bootstrap_succeeded}" == "1" ]]; then
    break
  fi
  sleep 1
done
if [[ "${bootstrap_succeeded:-}" != "1" ]]; then
  echo "bootstrap job did not succeed before timeout" >&2
  "${KCTL[@]}" get pods --namespace agp
  "${KCTL[@]}" logs job/agp-bootstrap --namespace agp --tail=200 || true
  exit 1
fi
"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/lease-sweeper --timeout=180s
"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/runtime-sweeper --timeout=180s
"${KCTL[@]}" wait --namespace agp --for=condition=available deployment/runtime --timeout=180s

cat > "${TMP_SMOKE_JOB_MANIFEST}" <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: agp-smoke
  namespace: agp
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: agp-smoke
    spec:
      restartPolicy: Never
      containers:
        - name: smoke
          image: agp:latest
          imagePullPolicy: Never
          command:
            - python
            - /app/scripts/smoke_local_stack.py
          envFrom:
            - configMapRef:
                name: agp-config
            - secretRef:
                name: agp-secrets
EOF

"${KCTL[@]}" delete job agp-smoke --namespace agp --ignore-not-found --wait=true
"${DOCKER[@]}" exec -i "${KIND_NODE_CONTAINER}" sh -lc "cat > '${NODE_SMOKE_JOB_MANIFEST}'" < "${TMP_SMOKE_JOB_MANIFEST}"
"${KCTL[@]}" apply -f "${NODE_SMOKE_JOB_MANIFEST}"

for _ in $(seq 1 180); do
  smoke_succeeded="$("${KCTL[@]}" get job agp-smoke --namespace agp -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  if [[ "${smoke_succeeded}" == "1" ]]; then
    break
  fi
  smoke_failed="$("${KCTL[@]}" get job agp-smoke --namespace agp -o jsonpath='{.status.failed}' 2>/dev/null || true)"
  if [[ -n "${smoke_failed}" && "${smoke_failed}" != "0" ]]; then
    echo "smoke job failed" >&2
    "${KCTL[@]}" logs job/agp-smoke --namespace agp --tail=200 || true
    exit 1
  fi
  sleep 1
done
if [[ "${smoke_succeeded:-}" != "1" ]]; then
  echo "smoke job did not succeed before timeout" >&2
  "${KCTL[@]}" get pods --namespace agp
  "${KCTL[@]}" logs job/agp-smoke --namespace agp --tail=200 || true
  exit 1
fi

echo
echo "Kubernetes smoke passed."
echo "Cluster: ${KIND_CLUSTER_NAME}"
echo "Overlay: ${KUSTOMIZE_PATH}"
