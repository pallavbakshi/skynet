# AGP Capability Registry Specification

## Status
Authoritative

## Purpose
Defines the capability blueprint schema, ownership, versioning, runtime compatibility, resource tier and permission profile model, and instantiation rules.

## Ownership
- capabilities are owned by the control plane
- runtimes do not define capability truth; they advertise compatibility with capabilities

## Capability Blueprint Fields
- `capability_id`
- `name`
- `version`
- `image_ref`
- `model_ref`
- `resource_tier`
- `permission_profile`
- `queue_mode`
- `runtime_requirements`

## Versioning
- capability versions are immutable once published
- changes require a new version
- old versions may be deprecated but remain referenceable until retired

## Compatibility
- runtimes advertise which capability versions they can host
- incompatibility excludes a runtime from claim eligibility

## Resource Tier
Defines expected compute envelope, for example:
- `small`
- `medium`
- `large`
- `gpu`

## Permission Profile
Defines execution restrictions, for example:
- read-only
- workspace-write
- network-restricted

## Instantiation Rules
- `agents/up` must reference a valid capability blueprint
- instantiated agent inherits capability constraints
- capability pool queues are keyed by `capability_id + version`
- retired capability versions remain referenceable for existing durable agents until explicitly migrated or terminated
