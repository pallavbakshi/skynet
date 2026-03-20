# AGP Artifact and Finalization Specification

## Status
Authoritative

## Purpose
This document defines:
- what becomes an artifact
- artifact durability rules
- artifact immutability rules
- terminal finalization flow
- orphan handling

## Artifact Principle
All material execution payloads that matter for auditability, debugging, or result consumption must be durable artifacts.

## Required Artifact Kinds
- `prompt`
- `transcript_log`
- `exec_log`
- `result`
- `failure_evidence`

## Durability Rules
The following must always be durable:
- original prompt payload
- transcript log
- exec log
- final result
- failure evidence, if any

## Immutability Rules
- Once an artifact is finalized and referenced by a terminal job/run state, it is immutable.
- Artifact content must not be overwritten in place.
- Replacement requires a new artifact ID.

## Storage Rules
- Artifact payloads live in the artifact store.
- State store contains only metadata and references.
- Artifact references must include:
  - `artifact_id`
  - `storage_ref`
  - `checksum`
  - `size_bytes`

## Finalization Flow

### Success Path
1. Runtime completes execution locally.
2. Runtime writes required artifacts to durable artifact storage.
3. Artifact store confirms persistence.
4. Runtime calls terminal success API with role-aware artifact references.
5. Control plane validates:
- valid lease
- valid fencing token
- required artifact roles exist
- artifact references exist
6. Control plane records artifact references in state store.
7. Control plane marks:
- run `completed`
- job `completed`

### Failure Path
1. Runtime determines terminal failure.
2. Runtime writes failure evidence and logs.
3. Artifact store confirms persistence.
4. Runtime calls terminal failure API with role-aware artifact references.
5. Control plane validates lease and token.
6. Control plane records artifact references in state store.
7. Control plane marks:
- run `failed`
- job `failed`

## Consistency Model
- Artifact write happens before terminal state commit.
- Terminal state commit must never point at a missing artifact.
- If artifact write succeeds but state-store finalization fails:
  - artifact may remain orphaned
  - run/job terminal state must not be committed

This is write-first finalization, not distributed two-phase commit.

## Orphan Handling
- Orphan artifacts are allowed.
- Orphan artifacts must be detectable by garbage collection.
- GC must never delete artifacts still referenced by authoritative state.

## Retrieval Rules
- `GET /artifacts/{artifact_id}` returns metadata.
- `GET /artifacts/{artifact_id}/content` returns content or retrieval location.
- Large artifact reads may be paginated or streamed.

## Provenance Rules
- Every artifact should be attributable to:
  - a job
  - a run
  - a role
- Result artifacts used in handoff must preserve provenance through handoff records.
- Terminal APIs must submit artifact references as `{artifact_id, role}` pairs.

## Failure Rules
- Stale lease owners may upload artifacts physically, but stale fencing tokens prevent them from publishing authoritative terminal state.
- Partial artifacts from failed runs remain inspectable.
