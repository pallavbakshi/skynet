"""Compatibility entrypoint for the split MVP flow suite."""

from tests.mvp_flow.test_core import MvpFlowCoreTest
from tests.mvp_flow.test_observability import MvpFlowObservabilityTest
from tests.mvp_flow.test_queue_security import MvpFlowQueueSecurityTest
from tests.mvp_flow.test_sweeps_recovery import MvpFlowSweepsRecoveryTest
from tests.mvp_flow.test_runtime_plugins import MvpFlowRuntimePluginsTest
from tests.mvp_flow.test_gap_regressions import MvpFlowGapRegressionTest

__all__ = [
    "MvpFlowCoreTest",
    "MvpFlowObservabilityTest",
    "MvpFlowQueueSecurityTest",
    "MvpFlowSweepsRecoveryTest",
    "MvpFlowRuntimePluginsTest",
    "MvpFlowGapRegressionTest",
]
