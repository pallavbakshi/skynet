"""smallops test bootstrap.

Live-style tests are opt-in by environment variable. Offline parser property
tests are always collected.
"""

from __future__ import annotations

import os

import pytest

from smallops_tests.helpers.artifacts import dump_failure_artifacts


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    gates = {
        "live": ("SMALLOPS_LIVE", "set SMALLOPS_LIVE=1"),
        "docker": ("SMALLOPS_DOCKER", "set SMALLOPS_DOCKER=1 (+ ANTHROPIC_API_KEY)"),
        "judge": ("SMALLOPS_JUDGE", "set SMALLOPS_JUDGE=1 (+ ANTHROPIC_API_KEY)"),
    }
    for item in items:
        for mark, (env, why) in gates.items():
            if mark in item.keywords and not os.environ.get(env):
                item.add_marker(pytest.mark.skip(reason=why))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    if not any(mark in item.keywords for mark in ("live", "docker", "judge")):
        return
    out_dir = dump_failure_artifacts(item)
    if out_dir is not None:
        report.sections.append(("smallops artifacts", str(out_dir)))
