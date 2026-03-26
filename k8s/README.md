# AGP Kubernetes Manifests

This manifest set is for deployment parity with the repo's current Phase 3 stack shape.
It is not an HA deployment.

## Included objects

- `Namespace`: `agp`
- `ConfigMap` and `Secret` for AGP environment
- PVCs for:
  - `/data` scratch storage
  - PostgreSQL data
  - MinIO object data
  - `/logs`
- `postgres` `Deployment` and `Service`
- `minio` `Deployment` and `Service`
- `redis` `Deployment` and `Service`
- `control-plane` `Deployment` and `Service`
- `runtime` `Deployment`
- `lease-sweeper` `Deployment`
- `runtime-sweeper` `Deployment`

## Important constraints

- The control plane is configured against a single PostgreSQL service, not a local SQLite file.
- `/data` remains mounted as scratch/local workspace storage and is no longer the primary state store.
- Do not scale `control-plane` above one replica with this manifest set until leader election and HA control-plane coordination are implemented.
- `agp-logs` requests `ReadWriteMany`. Your cluster storage class must support that, or this claim must be adapted.
- Agents self-register via `POST /agents/up` when their runtime starts — no bootstrap job is needed.

## Apply

First generate a real dev secret manifest instead of applying the placeholder values in [secret.yaml](/home/user/projects/skynet/k8s/secret.yaml):

```bash
./scripts/generate_k8s_dev_secret.sh /tmp/agp-k8s-secret.dev.yaml
kubectl apply -f /tmp/agp-k8s-secret.dev.yaml
```

Then apply the base manifests:

```bash
kubectl apply -k k8s
```

Validate the manifests without applying them:

```bash
python scripts/validate_phase3_assets.py
```

Reusable local setup helpers for Linux hosts:

```bash
./scripts/install_infra_tools.sh
./scripts/phase3_stack_up.sh
./scripts/phase3_stack_smoke.sh
./scripts/phase3_stack_down.sh
```

Local Kubernetes smoke for the single-node kind overlay:

```bash
./scripts/k8s_smoke.sh
```

The kind smoke uses [k8s/overlays/kind](/home/user/projects/skynet/k8s/overlays/kind) to force local-image usage and replace persistent volumes with `emptyDir` so the stack can be validated on a disposable single-node cluster.

Phase 3 backup and restore helpers:

```bash
uv run python scripts/phase3_backup_create.py /tmp/agp-phase3-backup
uv run python scripts/phase3_backup_restore.py /tmp/agp-phase3-backup
```

## Recommended next step

This manifest set now uses a shared PostgreSQL state store and a MinIO-backed S3-compatible artifact store. It still does not provide HA control-plane, HA Postgres, HA Redis, or HA object-store semantics. Treat it as a networked single-control-plane deployment, not a finished HA topology.
