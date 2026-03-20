# AGP Backup, Restore, and Disaster Recovery Specification

## Status
Operational

## Purpose
Defines backup scope, restore order, RPO/RTO handling, and state/artifact consistency during restore.

## Backup Scope
- state-store data
- artifact-store data
- queue/backlog reconstruction data from authoritative state
- configuration required to reconstruct service topology

## Restore Order
1. state-store
2. artifact-store availability validation
3. queue infrastructure
4. reconstruct queued backlog from authoritative state-store truth where necessary
5. control-plane services
6. runtimes

## RPO / RTO
- follow documented phase targets
- near-zero for control-plane state and backlog data
- up to one minute acceptable for replayable in-flight work

## Consistency Rules
- restored state must not reference missing artifacts
- restore validation must include artifact reference sampling or full consistency check
- orphan artifacts may remain after restore; missing referenced artifacts are not acceptable
- queued and retryable work must be reconstructable from state-store truth even if broker contents are not restored exactly

## DR Expectations
- failure drills must prove restore procedure
- restore runbooks must be current and executable
