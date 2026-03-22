# Product Requirements Document

## Document
Control Plane Modularization PRD

## Version
0.1 Draft

## Purpose
Refactor the AGP control plane from a monolithic file into a modular, testable server architecture without changing the external API surface or the core job / run / lease protocol.

This document exists to answer:
- how `control_plane.py` is split without destabilizing the platform
- what the target module boundaries are
- how route handlers stop owning business logic directly
- how API contracts become explicit instead of ad hoc dict serialization

## Why This Exists

Today, the AGP control plane is functionally strong but structurally overloaded.

[control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py) is currently the highest-risk file in the system:
- it is 3297 lines long
- it contains route definitions, auth middleware, exception handling, serialization helpers, queue coordination, sweeper logic, observability helpers, and direct ORM mutations
- there is no clean service layer between HTTP handlers and domain logic
- most responses are built manually through `_serialize(...)` and `response_model=dict`

This is slowing down further platform work in three ways:
1. changing one endpoint requires understanding unrelated logic in the same file
2. domain behavior is hard to unit test without going through FastAPI routes
3. the API contract is implicit and therefore easier to regress

## Goal

Deliver a modular control-plane architecture that:
- preserves the current external API behavior
- separates route handlers from business logic
- isolates auth and exception handling from endpoint definitions
- introduces typed request / response models for the core API surfaces
- reduces `control_plane.py` to a small application assembly module

## Non-Goals

- redesigning the AGP protocol
- converting the entire server to async I/O
- changing endpoint URLs or operator workflows
- replacing FastAPI
- redesigning skyops or the client SDK

## Scope

This PRD covers:
- decomposition of `control_plane.py`
- creation of route modules
- creation of service modules for core mutations
- centralization of auth middleware and exception handlers
- typed response models for high-value endpoints
- test migration to the new seams

This PRD does not cover:
- migration tooling
- queue backend redesign
- runtime plugin refactors outside the control-plane API boundary

## Current State

### Strengths To Preserve
- the API surface is already broad and useful
- control-plane behavior is proven in local, Docker, and K8s paths
- route-level functionality exists for jobs, runs, agents, runtimes, observability, security, and admin operations

### Structural Weaknesses
- `build_app()` defines middleware and exception handlers inline
- handlers directly execute domain transitions and SQLAlchemy logic
- serialization is repeated by field tuple rather than by explicit schema
- helper functions and route handlers are interleaved in one file

## Problem Statement

The control plane is no longer suffering from missing features. It is suffering from missing boundaries.

Without refactoring the control plane into composable modules:
- changes will continue to cluster in one file
- endpoint behavior will remain tightly coupled to ORM details
- tests will remain integration-heavy and slow to localize failures
- API contracts will remain weaker than they should be for a platform component

## Target Architecture

### Package Shape

```
src/agp/
  control_plane.py              # app assembly only
  api/
    app.py                      # FastAPI construction helpers
    middleware.py               # auth middleware
    errors.py                   # exception handlers + API error mapping
    schemas.py                  # shared request / response types
    routes/
      jobs.py
      runs.py
      agents.py
      runtimes.py
      artifacts.py
      observability.py
      security.py
      admin.py
  services/
    jobs.py
    runs.py
    leases.py
    agents.py
    runtimes.py
    artifacts.py
    events.py
    observability.py
```

### Route Responsibilities

Route modules own:
- request parsing
- dependency injection
- response shaping through typed schemas
- HTTP status code selection

Route modules do not own:
- lease transitions
- job state mutations
- run completion logic
- artifact linkage rules
- queue delivery decisions

### Service Responsibilities

Service modules own:
- domain validation
- ORM reads and writes
- event emission
- consistency checks across related models
- reusable business operations that may be called by routes or background sweepers

## Refactor Areas

### Area 1: Application Assembly Extraction

Current state:
- `build_app()` in `control_plane.py` builds the router, auth middleware, and exception handlers inline

Required change:
- move app construction to a small assembly layer
- move auth middleware to `api/middleware.py`
- move exception handlers to `api/errors.py`

Acceptance criteria:
- `control_plane.py` is primarily composition and app startup wiring
- auth middleware is independently unit tested
- exception handlers are independently unit tested

### Area 2: Route Decomposition

Current state:
- 50+ routes are defined in a single file

Required change:
- split routes by domain
- each route module exposes a router object
- route registration happens centrally during app assembly

Acceptance criteria:
- no route definitions remain in the monolithic file except optional health / root wiring
- jobs, runs, agents, runtimes, artifacts, observability, security, and admin routes each have a dedicated module

### Area 3: Service Layer Introduction

Current state:
- handlers execute direct SQLAlchemy mutation logic inline

Required change:
- introduce services for:
  - job creation / queueing / interrupt
  - run claim / completion / failure
  - lease creation / renewal / expiry / release
  - runtime registration / heartbeat / classification
  - artifact registration / linkage / validation
  - event creation

Acceptance criteria:
- route handlers delegate domain mutations into services
- sweepers reuse the same services instead of duplicating mutation logic
- service modules have targeted unit tests independent of FastAPI route tests

### Area 4: Typed API Responses

Current state:
- most endpoints return `dict`
- `_serialize(...)` manually selects fields

Required change:
- introduce typed response models for the highest-value surfaces first:
  - `/messages/send`
  - `/jobs/*`
  - `/runs/*`
  - `/agents/*`
  - `/runtimes/*`
  - `/artifacts/*`
  - `/observability/*`

Acceptance criteria:
- core endpoints use explicit response models instead of `dict`
- field selection logic is schema-driven rather than tuple-driven
- OpenAPI output is materially aligned with runtime behavior

## Implementation Strategy

### Phase 1
- extract auth middleware and exception handlers
- extract shared serialization / paging helpers into `api/` or `services/`
- create route modules without changing internals yet

### Phase 2
- move mutation-heavy logic into service modules
- keep route behavior stable while reducing direct ORM logic in handlers

### Phase 3
- replace manual response dict assembly for core endpoints with typed response models
- split route tests from service tests

## Acceptance Tests

The refactor is complete when:
- existing end-to-end control-plane behavior remains green
- route modules are separated by domain
- auth middleware is tested outside full app boot
- core services have unit tests for success and failure paths
- the public API still passes existing client and skyops integration tests

## Risks

### Risk 1: Hidden Behavior Change During Extraction
- the current file contains subtle helper interactions
- mitigation: keep route signatures stable and move code in small vertical slices

### Risk 2: Over-abstracting Too Early
- a heavy framework-style service layer could make the code worse
- mitigation: use thin, domain-focused services tied to existing protocol concepts

### Risk 3: Refactoring Without Better Tests
- module extraction without targeted tests will create false confidence
- mitigation: each extracted service must gain direct tests before deleting old coverage paths

## Deliverables

- modular route tree under `src/agp/api/routes/`
- service layer under `src/agp/services/`
- app assembly split from domain logic
- typed response models for core surfaces
- reduced `control_plane.py` acting as composition only

## Success Criteria

This PRD is complete when:
- `control_plane.py` is no longer the main locus of domain logic
- new platform features can be added by touching a route module and one service module, not a monolith
- API contracts for core endpoints are explicit and test-backed
