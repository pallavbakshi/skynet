# AGP Upgrade and Rollback Specification

## Status
Operational

## Purpose
Defines schema migration policy, version-skew policy, rollback constraints, and artifact compatibility expectations.

## Schema Migration Policy
- state-store schema changes must be explicitly versioned
- destructive migrations require planned procedure
- migrations should preserve protocol invariants during rollout

## Version Skew Policy
- control plane and runtimes may temporarily operate at bounded skew during rollout only if protocol compatibility is maintained
- unsupported skew must block rollout
- supported skew window:
  - control plane may be at most one minor version ahead of runtimes during rolling upgrade
  - runtimes may not be ahead of the control plane
  - major-version skew is unsupported

## Rollback Constraints
- rollback must not corrupt state-store truth
- rollback must preserve artifact readability
- rollback plans must be documented before rollout

## Artifact Compatibility
- artifact formats referenced by terminal state must remain readable after upgrade
- incompatible artifact changes require versioning strategy

## Rollback Window
- rollback must target the immediately previous supported release unless an explicit migration reversal path is documented
