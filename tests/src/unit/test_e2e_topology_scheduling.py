"""A combined worker must never switch back after installing the component."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..e2e.basic.test_backend_dispatch_smoke import (
    _SKIP_CEILING_PER_LANE,
    test_session_skipped_count_below_ceiling as check_skip_ceiling,
)


def test_installed_phase_excess_skips_fail_the_combined_gate(monkeypatch):
    monkeypatch.setenv("E2E_COMBINE_TOPOLOGIES", "1")
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    absent = SimpleNamespace(
        callspec=SimpleNamespace(params={"e2e_topology": "topology_without_tools"}),
        iter_markers=lambda: iter(()),
    )
    installed = SimpleNamespace(
        callspec=SimpleNamespace(params={"e2e_topology": "topology_with_tools"}),
        iter_markers=lambda: iter((pytest.mark.skip.mark,)),
    )
    request = SimpleNamespace(
        node=absent,
        session=SimpleNamespace(
            items=[absent] + [installed] * (_SKIP_CEILING_PER_LANE["container"] + 1)
        ),
    )
    with pytest.raises(AssertionError, match="exceeds the ceiling"):
        check_skip_ceiling(request, {"backend": "container"})


@pytest.mark.parametrize(
    ("workers", "backend", "expected"), [(0, "", 18), (2, "", 18), (2, "embedded", 15)]
)
def test_component_installation_is_one_way_per_worker(
    tmp_path: Path, workers: int, backend: str, expected: int
) -> None:
    """Exercise real pytest session parameters and xdist scope dispatch together."""
    root = Path(__file__).resolve().parents[3]
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    requires_tools_entry\n    component_topology\n"
        "    requires_component_surface\n    installs_component_surface\n"
    )
    (tmp_path / "conftest.py").write_text("""
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
""")
    for name in ("test_first", "test_second", "test_third"):
        (tmp_path / f"{name}.py").write_text("""
import os
import pytest

def test_general_coverage_runs_once(e2e_topology):
    assert e2e_topology == "topology_without_tools"

@pytest.mark.requires_tools_entry
def test_privileged_services_run_after_installation(e2e_topology):
    assert e2e_topology == "topology_with_tools"

@pytest.mark.installs_component_surface
def test_enabling_server_entry_cannot_contaminate_absent_phase(e2e_topology):
    assert e2e_topology == "topology_with_tools"

@pytest.mark.component_topology
def test_component_and_fallback_paths(e2e_topology):
    assert e2e_topology in ("topology_without_tools", "topology_with_tools")

@pytest.mark.requires_component_surface
def test_shared_surface_runs_when_available(e2e_topology):
    expected = "topology_without_tools" if os.environ.get("E2E_BACKEND") == "embedded" else "topology_with_tools"
    assert e2e_topology == expected
""")
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join(
            (str(root), str(root / "src"), str(root / "tests"), str(root / "tests/src"))
        ),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "E2E_COMBINE_TOPOLOGIES": "1",
        "E2E_BACKEND": backend,
        "HAOS_TEST_MODE": "",
        "HA_MCP_CONFIG_DIR": str(tmp_path / "settings"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-n",
            str(workers),
            "--dist",
            "loadscope",
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{expected} passed" in result.stdout, result.stdout
    traces = [path.read_text().splitlines() for path in tmp_path.glob("*.trace")]
    assert traces
    # A worker may finish its absent scopes while another consumes all the
    # smaller installed scopes. Only workers assigned those scopes install.
    allowed = (
        ["topology_without_tools"],
        ["topology_with_tools"],
        ["topology_without_tools", "topology_with_tools"],
    )
    assert all(trace in allowed for trace in traces), traces
    assert {phase for trace in traces for phase in trace} == {
        "topology_without_tools",
        "topology_with_tools",
    }
