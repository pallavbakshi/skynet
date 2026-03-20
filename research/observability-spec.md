# AGP Observability Specification

## Status
Operational

## Purpose
Defines required metrics, traces, structured logs, alert conditions, dashboards, and retention expectations.

## Required Metrics

### Control Plane
- API latency
- queue depth
- lease churn
- interrupt rate
- job throughput

### Runtime
- heartbeat success/failure
- local recovery count
- crash count
- artifact upload failures

### Storage / Queue
- queue redelivery rate
- state-store latency/errors
- artifact-store latency/errors

## Required Traces
- `send -> enqueue -> claim -> lease -> run -> artifact finalization -> terminal state`

## Required Structured Logs
- control-plane lifecycle logs
- runtime supervision logs
- transcript logs
- exec logs

## Alert Conditions
- control-plane unavailable
- queue unavailable
- state-store unavailable
- artifact-store unavailable
- heartbeat loss spike
- repeated fencing events
- rising terminal failure rate

## Dashboard Expectations
- platform health overview
- runtime fleet view
- active jobs view
- failure triage view

## Retention
- metrics: minimum 30 days
- traces: minimum 14 days
- control-plane structured logs: minimum 30 days
- audit records: minimum 365 days
- transcript logs: minimum 90 days
- exec logs: minimum 90 days
- terminal result artifacts and failure evidence: minimum 365 days unless superseded by a stricter compliance policy
