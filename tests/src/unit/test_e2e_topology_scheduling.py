"""A combined worker must never switch back after installing the component."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def test_installed_phase_excess_skips_fail_the_combined_gate(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A combined lane keeps the component-present skip ceiling.

    Its absent phase sets E2E_NO_TOOLS_ENTRY, so reading the lane key from that
    alone would grant the much larger no-tools allowance and hide installed-
    phase tests that skip. The real smoke check runs here in a combined
    session whose installed phase skips one test more than the ceiling.
    """
    monkeypatch.setenv("E2E_COMBINE_TOPOLOGIES", "1")
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "0")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("E2E_BACKEND", raising=False)
    monkeypatch.delenv("HAOS_TEST_MODE", raising=False)
    pytester.makeini("[pytest]\nmarkers =\n    requires_tools_entry\n")
    pytester.makeconftest(
        """
        import pytest
        from tests.src.e2e._conftest_topology import (
            order_topologies, pytest_configure, pytest_generate_tests,
        )

        def pytest_collection_modifyitems(items):
            order_topologies(items)

        @pytest.fixture(scope="session", autouse=True)
        def e2e_topology(request):
            yield request.param

        @pytest.fixture(scope="session")
        def ha_container_with_fresh_config():
            return {"backend": "container"}
        """
    )
    pytester.makepyfile(
        """
        import pytest
        from tests.src.e2e.basic.test_backend_dispatch_smoke import (
            SKIP_CEILING_PER_LANE,
            test_session_skipped_count_below_ceiling,
        )

        @pytest.mark.requires_tools_entry
        @pytest.mark.skip(reason="installed-phase skip")
        @pytest.mark.parametrize("n", range(SKIP_CEILING_PER_LANE["container"] + 1))
        def test_installed_only(n):
            pass
        """
    )

    result = pytester.runpytest("-p", "no:cacheprovider")

    result.stdout.fnmatch_lines(["*exceeds the ceiling*"])
    assert result.parseoutcomes()["failed"] == 1


def _run_combined_session(
    tmp_path: Path, workers: int, dist: str, backend: str = ""
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[3]
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
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-n",
            str(workers),
            "--dist",
            dist,
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_scheduler_that_splits_modules_across_phases_is_refused(
    tmp_path: Path,
) -> None:
    """`--dist load` hands single tests to any worker, so one could install the
    component while another still runs the module's absent-phase tests."""
    (tmp_path / "conftest.py").write_text(
        "from tests.src.e2e._conftest_topology import pytest_configure\n"
    )
    (tmp_path / "test_any.py").write_text("def test_any():\n    pass\n")

    result = _run_combined_session(tmp_path, 2, "load")

    assert result.returncode != 0
    assert "Combined topologies require --dist loadscope" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    ("workers", "backend", "expected"), [(0, "", 18), (2, "", 18), (2, "embedded", 15)]
)
def test_component_installation_is_one_way_per_worker(
    tmp_path: Path, workers: int, backend: str, expected: int
) -> None:
    """Exercise real pytest session parameters and xdist scope dispatch together."""
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
    result = _run_combined_session(tmp_path, workers, "loadscope", backend)
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
