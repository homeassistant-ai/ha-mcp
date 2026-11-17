"""A combined worker must never switch back after installing the component."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("workers", [0, 2])
def test_component_installation_is_one_way_per_worker(tmp_path: Path, workers: int) -> None:
    """Exercise real pytest session parameters and xdist scope dispatch together."""
    root = Path(__file__).resolve().parents[3]
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    requires_tools_entry\n    component_topology\n"
    )
    (tmp_path / "conftest.py").write_text('''
import os
from pathlib import Path
import pytest
from tests.src.e2e._conftest_topology import (
    pytest_configure, pytest_generate_tests, pytest_xdist_make_scheduler,
    order_topologies,
)

def pytest_collection_modifyitems(items):
    order_topologies(items)

@pytest.fixture(scope="session", autouse=True)
def e2e_topology(request):
    phase = request.param
    trace = Path(__file__).parent / (os.environ.get("PYTEST_XDIST_WORKER", "serial") + ".trace")
    previous = trace.read_text().splitlines() if trace.exists() else []
    assert not (previous and previous[-1] == "topology_with_tools" and phase == "topology_without_tools")
    with trace.open("a") as stream:
        stream.write(phase + "\\n")
    yield phase
''')
    for name in ("test_first", "test_second", "test_third"):
        (tmp_path / f"{name}.py").write_text('''
import pytest

def test_general_coverage_runs_once(e2e_topology):
    assert e2e_topology == "topology_without_tools"

@pytest.mark.requires_tools_entry
def test_privileged_services_run_after_installation(e2e_topology):
    assert e2e_topology == "topology_with_tools"

@pytest.mark.component_topology
def test_component_and_fallback_paths(e2e_topology):
    assert e2e_topology in ("topology_without_tools", "topology_with_tools")
''')
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join((str(root), str(root / "src"), str(root / "tests"), str(root / "tests/src"))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "E2E_COMBINE_TOPOLOGIES": "1",
        "HA_MCP_CONFIG_DIR": str(tmp_path / "settings"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-n", str(workers), "--dist", "loadscope", "-q"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "12 passed" in result.stdout, result.stdout
    traces = [path.read_text().splitlines() for path in tmp_path.glob("*.trace")]
    assert traces
    assert all(trace == ["topology_without_tools", "topology_with_tools"] for trace in traces)
