# Product Requirements Document

## Document
Schema And State Hardening PRD

## Version
0.1 Draft

## Purpose
Unify database schema authority, harden state modeling, and remove the drift between ORM definitions, raw migration SQL, and runtime assumptions.

This document exists to answer:
- how AGP moves from `create_all()` bootstrapping to explicit migrations
- how status and enum-like fields become consistently validated
- how event sequencing and persisted platform metadata become database-owned rather than process-local

## Why This Exists

AGP’s runtime behavior has outgrown its schema discipline.

Today:
- [db.py](/home/user/projects/skynet/src/agp/db.py) still uses `Base.metadata.create_all(...)`
- [0001_initial.sql](/home/user/projects/skynet/migrations/0001_initial.sql) also defines schema and constraints
- many ORM columns are plain `String`
- event sequence generation uses process-local mutable state in [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)

This creates four risks:
1. SQLite/dev schema and migration-defined schema can drift
2. enum-like fields are weakly validated in the ORM path
3. the event sequence mechanism does not scale cleanly across multiple processes
4. schema evolution remains operationally fragile

## Goal

Deliver a single, authoritative schema evolution path and stronger persisted invariants for AGP state transitions.

The end state must:
- use migrations as the schema authority
- enforce state validity consistently
- move event sequencing to the database
- ensure all supported environments derive schema from the same contract

## Non-Goals

- redesigning the AGP data model from scratch
- changing business semantics of jobs, runs, leases, or artifacts
- introducing multi-tenant sharding or cross-region replication
- rewriting all queries to async

## Scope

This PRD covers:
- migration framework adoption
- removal of long-term dependence on `create_all()`
- enum / constraint hardening for state fields
- event sequence generation refactor
- schema version management

This PRD does not cover:
- route modularization
- queue backend redesign
- operator CLI refactors outside schema lifecycle operations

## Current State

### Strengths To Preserve
- the current schema already models the core entities well
- the initial SQL migration contains many useful `CHECK` constraints
- system metadata already tracks `schema_version` and `release_version`

### Weaknesses To Fix
- two schema authorities exist
- ORM definitions do not reflect the same degree of constraint as the SQL migration
- process-local event sequence state is a poor long-term fit
- schema change delivery is not yet operationalized

## Problem Statement

AGP’s data model is conceptually sound, but its enforcement and evolution model are not yet disciplined enough for a platform that claims durable orchestration semantics.

Without hardening schema authority:
- local and deployed environments can diverge
- enum/state bugs remain easier to introduce than they should be
- migration work will get harder with every additional table or state machine

## Target State

### Migration Authority

There is one supported schema evolution path:
- migrations define the database contract
- new tables and alterations ship through that path
- local init commands apply migrations rather than creating schema ad hoc

### State Enforcement

Status-like fields are enforced consistently through one of:
- SQLAlchemy enum columns, or
- explicit `CheckConstraint` declarations mirrored in migrations

The exact mechanism may vary by backend compatibility, but the enforcement must be explicit and test-backed.

### Event Sequencing

Monotonic event sequence allocation is database-owned.

There must be no process-local global counter used as the authoritative event sequence source.

## Refactor Areas

### Area 1: Migration Framework Adoption

Current state:
- migration SQL exists
- runtime bootstrap still uses `create_all()`

Required change:
- adopt an explicit migration toolchain
- make `initdb` and future `db migrate` flow through migrations
- preserve SQLite support deliberately rather than accidentally

Acceptance criteria:
- schema bootstrapping for supported environments uses migrations
- schema version advancement is observable and explicit
- developer setup remains simple enough for local use

### Area 2: ORM / Migration Contract Alignment

Current state:
- ORM columns are mostly `String`
- raw SQL expresses stronger constraints than the ORM

Required change:
- align ORM declarations with intended state constraints
- document which constraints are database-enforced vs application-enforced
- eliminate silent divergence between model intent and migration reality

Acceptance criteria:
- core state fields have explicit validity rules
- migration definitions and ORM definitions do not contradict each other

### Area 3: Event Sequence Ownership

Current state:
- event sequencing uses `_event_seq_lock` and `_event_seq_counter`

Required change:
- allocate event sequence numbers through the database
- remove process-local event sequence authority

Acceptance criteria:
- event sequence generation works correctly across multiple server processes
- no mutable module-level counter remains authoritative

### Area 4: Schema Lifecycle Commands

Current state:
- `initdb` implies bootstrap, not lifecycle discipline

Required change:
- establish clear operator semantics for:
  - initialize empty schema
  - inspect current version
  - apply pending migrations
  - fail clearly on incompatible versions

Acceptance criteria:
- local bare-metal, Docker, and K8s startup paths all use the same schema lifecycle assumptions
- skyops database commands are aligned with the migration system

## Design Principles

### Principle 1: One Source Of Truth
- migrations own schema evolution
- ORM models express application structure, not a separate hidden schema contract

### Principle 2: Preserve Developer Velocity
- local setup cannot become materially harder than it is today
- migration workflow must still support fast local iteration

### Principle 3: Database-Owned Invariants Beat Process-Owned Invariants
- event sequencing, state validity, and uniqueness rules should live in the database where practical

## Acceptance Tests

The refactor is complete when:
- a fresh local environment initializes via migrations
- an upgraded environment applies pending migrations cleanly
- invalid status transitions or invalid state values are rejected by the right layer
- event sequence allocation remains monotonic across repeated process restarts and multi-process operation

## Risks

### Risk 1: SQLite / Postgres Behavior Drift
- a migration strategy can accidentally become Postgres-only
- mitigation: define supported local behavior explicitly and test it

### Risk 2: Overusing SQL Enums
- native enums can complicate migrations across environments
- mitigation: `CheckConstraint` is acceptable where it yields a simpler lifecycle

### Risk 3: Breaking Existing Boot Paths
- `initdb`, Docker, and K8s startup scripts all depend on current behavior
- mitigation: migrate those paths intentionally as part of the rollout, not afterward

## Deliverables

- migration framework integrated into AGP
- schema lifecycle commands aligned to migrations
- aligned ORM and schema constraints
- database-owned event sequence generation
- test coverage for schema init, upgrade, and state enforcement

## Success Criteria

This PRD is complete when:
- AGP no longer has two competing schema authorities
- state invariants are explicit instead of implied
- event sequencing is safe across more than one server process
