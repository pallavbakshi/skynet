# AGP Queue Backend Integration Specification

## Status
Authoritative

## Purpose
Defines the boundary between AGP queue semantics and the concrete broker implementation.

This document covers:
- acknowledgement strategy
- redelivery timeout mapping
- poison / dead-letter policy
- broker-specific assumptions

## Principle
The broker is transport, not truth. Broker behavior must be mapped onto AGP's authoritative state-store and lease semantics rather than treated as the source of truth.

## Acknowledgement Strategy
- A delivered message must not be acknowledged as durably consumed until the control plane has accepted the claim and created authoritative run/lease state.
- Empty or ineligible deliveries may be negatively acknowledged, released, or allowed to time out depending on broker behavior.
- Terminal success or failure is determined by control-plane state, not by broker acknowledgment.

## Redelivery Timeout Mapping
- Broker redelivery timeout is advisory and must not replace AGP lease expiry logic.
- Lease expiry remains controlled by AGP heartbeat rules.
- Broker visibility timeout or unacked-message timeout should be configured:
  - longer than the normal heartbeat interval
  - shorter than unacceptable execution-stall windows

## Poison / Dead-Letter Policy
- Messages that repeatedly redeliver without successful authoritative claim or repeatedly fail broker-level handling may be moved to a dead-letter path.
- Dead-lettering must not erase authoritative job truth from the state store.
- Operators must be able to correlate dead-lettered broker messages with AGP job identity when possible.

## Broker-Specific Assumptions
- At-least-once delivery is assumed.
- Broker-side duplication is expected.
- Broker-side exactly-once semantics are not required.
- Broker ordering is only trusted within the limits of AGP queue-topology rules.

## Integration Invariants
- Broker redelivery must never create duplicate active runs.
- Unacked broker deliveries must not override control-plane truth.
- Broker outage must not destroy queued-work truth because queued jobs remain represented in the state store.
