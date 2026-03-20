# AGP Kubernetes Manifests

This manifest set is for deployment parity with the local `compose.yaml` stack.
It is not an HA deployment.

## Included objects

- `Namespace`: `agp`
- `ConfigMap` and `Secret` for AGP environment
- PVCs for:
  - `/data`
  - `/artifacts`
  - `/logs`
- `redis` `Deployment` and `Service`
- `control-plane` `Deployment` and `Service`
- `agp-bootstrap` `Job`
- `runtime` `Deployment`
- `lease-sweeper` `Deployment`
- `runtime-sweeper` `Deployment`

## Important constraints

- The control plane is configured against a single SQLite database file on `/data/agp.db`.
- Do not scale `control-plane` above one replica with this manifest set.
- `agp-artifacts` and `agp-logs` request `ReadWriteMany`. Your cluster storage class must support that, or these claims must be adapted.
- `agp-bootstrap` depends on the control plane becoming healthy. The bootstrap script already waits on `/health` before creating the default capability and durable agent.

## Apply

```bash
kubectl apply -k k8s
```

## Recommended next step

If you want a real Phase 3 topology, move the state store off SQLite first. Until then, treat this as a single-control-plane cluster deployment, not an HA control plane.
