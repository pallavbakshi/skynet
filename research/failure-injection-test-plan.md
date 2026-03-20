# AGP Failure Injection Test Plan

## Status
Operational

## Purpose
Defines failure cases that must be exercised to validate AGP correctness and resilience.

## Core Failure Cases
- runtime crash during active run
- lease expiry due to heartbeat loss
- duplicate terminal report replay
- queue redelivery after consumer restart
- artifact-store write failure before terminal report
- DB/state-store failure during finalization
- runtime replacement during queued and running work

## Additional Cases
- forced agent teardown while busy
- control-plane restart during active work
- queue restart during backlog processing
- artifact-store latency spike
- repeated fencing requests against stale owner

## Expected Assertions
- no duplicate authoritative terminal state
- no terminal job points to missing artifact
- retry creates new run rather than mutating old run
- forced teardown yields terminal cancellation
- redelivery does not violate state-store truth
- restored or replacement runtimes can resume claiming safely
