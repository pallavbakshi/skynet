"""Operator helper functions — backup, drills, maintenance, upgrades.

Re-exports all public functions for backward compatibility with
``from agp._ops_helpers import ...`` call sites.
"""

from agp._ops_helpers._backup import (  # noqa: F401
    create_backup_snapshot,
    restore_and_recover_snapshot,
    restore_backup_snapshot,
    validate_restored_state,
)
from agp._ops_helpers._drill import run_failure_injection_scenario  # noqa: F401
from agp._ops_helpers._maintenance import (  # noqa: F401
    detect_orphan_artifacts,
    prune_observability_logs,
    reconstruct_queue_from_state,
)
from agp._ops_helpers._upgrade import (  # noqa: F401
    get_upgrade_status,
    mark_upgrade,
    rollback_to_previous_version,
)
