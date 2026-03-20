# AGP Handoff and Provenance Specification

## Status
Authoritative

## Purpose
Defines handoff behavior, source-job requirements, fan-out rules, artifact selection, lineage, and invalid handoffs.

## Handoff Definition
A handoff is the creation of one or more follow-on jobs from the durable artifacts of a source job.

## Source Requirements
- source job must exist
- source job should normally be terminal before handoff
- source artifacts must be explicit

## Fan-Out
- one handoff may create multiple child jobs
- each child job is independent and receives its own `job_id`

## Artifact Selection Rules
- handoff request must explicitly name source artifact IDs
- only durable artifacts may be handed off
- result artifacts are the default, but logs may also be selected if policy permits

## Lineage
- handoff records link:
  - source job
  - source artifacts
  - child jobs

## Invalid Handoffs
- handoff from nonexistent job
- handoff from missing artifacts
- handoff to terminated agent
- handoff that would create direct self-loop on same job

## Cycles
- cyclic handoff chains should be detectable and rejectable by policy
- minimum rule: a job may not be an ancestor and direct child within the same handoff chain
