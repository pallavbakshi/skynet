# AGP Kubernetes Manifests

This manifest set is for deployment parity with the repo's current Phase 3 stack shape.
It is not an HA deployment.

## Included objects

- `Namespace`: `agp`
- `ConfigMap` and `Secret` for AGP environment
- PVCs for:
  - `/data` scratch storage
  - PostgreSQL data
  - `/artifacts`
  - `/logs`
- `postgres` `Deployment` and `Service`
- `redis` `Deployment` and `Service`
- `control-plane` `Deployment` and `Service`
- `agp-bootstrap` `Job`
- `runtime` `Deployment`
- `lease-sweeper` `Deployment`
- `runtime-sweeper` `Deployment`

## Important constraints

- The control plane is configured against a single PostgreSQL service, not a local SQLite file.
- `/data` remains mounted as scratch/local workspace storage and is no longer the primary state store.
- Do not scale `control-plane` above one replica with this manifest set until leader election and HA control-plane coordination are implemented.
- `agp-artifacts` and `agp-logs` request `ReadWriteMany`. Your cluster storage class must support that, or these claims must be adapted.
- `agp-bootstrap` depends on the control plane becoming healthy. The bootstrap script already waits on `/health` before creating the default capability and durable agent.

## Apply

```bash
kubectl apply -k k8s
```

Validate the manifests without applying them:

```bash
python scripts/validate_phase3_assets.py
```

## Recommended next step

This manifest set now uses a shared PostgreSQL state store. It still does not provide HA control-plane or HA Postgres semantics. Treat it as a networked single-control-plane deployment, not a finished HA topology.
