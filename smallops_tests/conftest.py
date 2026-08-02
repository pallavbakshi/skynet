"""smallops test bootstrap.

Live-style tests are opt-in by environment variable. Offline parser property
tests are always collected.
"""

from __future__ import annotations

import os

import pytest

from smallops_tests.helpers.artifacts import (
    dump_docker_diagnostics,
    dump_failure_artifacts,
)
from smallops_tests.helpers.harness import SMALLOPS_MUXES


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "smallops_mux" in metafunc.fixturenames:
        metafunc.parametrize("smallops_mux", SMALLOPS_MUXES)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    gates = {
        "live": ("SMALLOPS_LIVE", "set SMALLOPS_LIVE=1"),
        "docker": ("SMALLOPS_DOCKER", "set SMALLOPS_DOCKER=1 (+ Anthropic or OpenRouter credentials)"),
        "judge": ("SMALLOPS_JUDGE", "set SMALLOPS_JUDGE=1 (+ ANTHROPIC_API_KEY)"),
    }
    for item in items:
        for mark, (env, why) in gates.items():
            if mark in item.keywords and not os.environ.get(env):
                item.add_marker(pytest.mark.skip(reason=why))
        if "docker" in item.keywords and not (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("SMALLOPS_CODEX_OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, OPENROUTER_API_KEY, "
                        "SMALLOPS_CODEX_OPENROUTER_API_KEY, or OPENAI_API_KEY"
                    )
                )
            )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    if not any(mark in item.keywords for mark in ("live", "docker", "judge")):
        return
    out_dir = dump_failure_artifacts(item)
    if "docker" in item.keywords:
        docker_dir = dump_docker_diagnostics(item, out_dir=out_dir)
        if docker_dir is not None:
            report.sections.append(("smallops docker diagnostics", str(docker_dir)))
    if out_dir is not None:
        report.sections.append(("smallops artifacts", str(out_dir)))
