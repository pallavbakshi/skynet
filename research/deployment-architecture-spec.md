# AGP Deployment Architecture Specification

## Status
Operational

## Purpose
Defines service topology, HA assumptions, singleton vs replicated services, and managed vs self-hosted dependencies.

## Core Services
- `control-plane`
- `queue`
- `state-store`
- `artifact-store`
- `runtimes`

## HA Assumptions
- control plane should be replicated behind stable service addressing
- state store and queue require HA or managed equivalents
- artifact store must be durable and redundant
- runtimes are horizontally replaceable and individually non-HA

## Singleton vs Replicated
- `control-plane`: replicated preferred
- `queue`: HA deployment or managed service
- `state-store`: HA deployment or managed service
- `artifact-store`: managed or replicated durable storage
- `runtimes`: replicated fleet

## Managed vs Self-Hosted
- queue, database, and artifact storage may be managed services if they preserve required semantics
- service choice must not change AGP protocol semantics
