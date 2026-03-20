# AGP Security Model Specification

## Status
Operational

## Purpose
Defines runtime identity, service auth, operator authz, secret rotation, encryption, and artifact/state access boundaries.

## Service Identity
- each runtime must have an authenticated service identity
- control plane must authenticate runtime identity before accepting runtime APIs

## Service Authentication
- runtime-to-control-plane traffic must be authenticated
- service-to-service traffic must be encrypted in transit
- V1 uses bearer-token authentication over HTTPS or another encrypted transport
- runtimes authenticate with the configured runtime bearer token
- operator/orchestration clients authenticate with the configured operator bearer token
- mTLS-backed service identity is a later-phase enhancement and is not part of the V1 implementation contract

## Operator Authorization
- operator access to state, artifacts, and destructive actions must be role-controlled
- destructive actions such as forced agent down must be auditable
- operator roles must at minimum distinguish:
  - read-only inspection
  - interrupt / cancel
  - agent lifecycle control
  - credential and security administration

## Secret Rotation
- runtime credentials and service secrets must be rotatable
- no hardcoded static credentials in application config
- token rotation procedures must be defined and testable
- rotation must support replacing the runtime token and operator token independently

## Storage Access Boundaries
- artifact store and state store must enforce access boundaries
- application components should use least-privilege credentials

## Audit Expectations
- privileged actions
- runtime registration
- credential changes
- destructive interrupts/teardowns
must be auditable

## V1 Boundary
- `/health` may remain unauthenticated
- all other control-plane HTTP surfaces are authenticated
- a runtime token must not authorize orchestration/operator endpoints
- an operator token must not authorize runtime write endpoints
