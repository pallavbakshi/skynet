"""Compatibility entrypoint for the split MVP flow suite."""

from tests.mvp_flow.core import MvpFlowCoreTest
from tests.mvp_flow.observability import MvpFlowObservabilityTest
from tests.mvp_flow.queue_security import MvpFlowQueueSecurityTest
from tests.mvp_flow.sweeps_recovery import MvpFlowSweepsRecoveryTest
from tests.mvp_flow.runtime_plugins import MvpFlowRuntimePluginsTest
from tests.mvp_flow.gap_regressions import MvpFlowGapRegressionTest

__all__ = [
    "MvpFlowCoreTest",
    "MvpFlowObservabilityTest",
    "MvpFlowQueueSecurityTest",
    "MvpFlowSweepsRecoveryTest",
    "MvpFlowRuntimePluginsTest",
    "MvpFlowGapRegressionTest",
]
