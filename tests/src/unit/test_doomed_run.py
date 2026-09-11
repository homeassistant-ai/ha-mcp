"""Unit tests for the e2e fail-fast ``DoomedRunDetector``.

Covers the abort threshold, reset-on-real-pass/fail semantics, rerun/skip
exclusions, and retention of the original fixture failure when an xdist worker
aborts while another worker is passing tests.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.src.doomed_run import DoomedRunDetector


def test_call_pass_resets_streak():
    d = DoomedRunDetector(threshold=5)
    for _ in range(4):
        assert d.record("setup", "failed") is False
    assert d.streak == 4
    assert d.record("call", "passed") is False
    assert d.streak == 0


def test_call_fail_resets_streak():
    d = DoomedRunDetector(threshold=5)
    for _ in range(4):
        d.record("setup", "failed")
    assert d.record("call", "failed") is False
    assert d.streak == 0


def test_rerun_outcome_neither_resets_nor_increments():
    d = DoomedRunDetector(threshold=5)
    d.record("setup", "failed")
    d.record("setup", "failed")
    assert d.streak == 2
    assert d.record("setup", "rerun") is False  # pytest-rerunfailures
    assert d.record("call", "rerun") is False
    assert d.streak == 2


def test_aborts_exactly_at_threshold():
    d = DoomedRunDetector(threshold=50)
    for _ in range(49):
        assert d.record("setup", "failed") is False
    assert d.record("setup", "failed") is True
    assert d.streak == 50


def test_interleaved_pass_prevents_abort():
    d = DoomedRunDetector(threshold=5)
    for _ in range(10):
        for _ in range(4):
            assert d.record("setup", "failed") is False
        assert d.record("call", "passed") is False
    assert d.streak == 0


def test_teardown_error_counts_toward_streak():
    d = DoomedRunDetector(threshold=2)
    assert d.record("teardown", "failed") is False
    assert d.record("teardown", "failed") is True


def test_skip_and_passing_setup_do_not_increment():
    d = DoomedRunDetector(threshold=2)
    assert d.record("setup", "skipped") is False
    assert d.record("setup", "passed") is False
    assert d.streak == 0


def test_default_threshold_is_50():
    assert DoomedRunDetector().threshold == 50


def test_xdist_abort_retains_first_shared_fixture_failure(tmp_path):
    """A worker exit must not hide the exception that doomed that worker."""
    repo_root = Path(__file__).resolve().parents[3]
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    # Register the actual E2E hook, without registering its HA/container fixtures.
    (tmp_path / "conftest.py").write_text(
        "from tests.src.e2e.conftest import pytest_runtest_logreport\n",
        encoding="utf-8",
    )
    (tmp_path / "test_broken.py").write_text(
        textwrap.dedent(
            """\
            import time
            from pathlib import Path
            import pytest

            @pytest.fixture(scope="session")
            def broken_session():
                deadline = time.monotonic() + 10
                while not Path("healthy-started").exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("healthy worker did not start")
                    time.sleep(0.01)
                raise RuntimeError("ORIGINAL_" + "SESSION_SETUP_SENTINEL")

            @pytest.mark.parametrize("case", range(55))
            def test_broken_session(case, broken_session):
                pass
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "test_healthy.py").write_text(
        textwrap.dedent(
            """\
            import time
            from pathlib import Path
            import pytest

            @pytest.mark.parametrize("case", range(100))
            def test_healthy(case):
                Path("healthy-started").touch()
                time.sleep(0.01)
            """
        ),
        encoding="utf-8",
    )
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(repo_root), str(repo_root / "src")))
    env["HAMCP_ENV_FILE"] = str(empty_env)
    env["HA_MCP_CONFIG_DIR"] = str(tmp_path / "config")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-n2",
            "--dist=loadscope",
            "-vv",
            "--confcutdir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    (tmp_path / "subprocess.log").write_text(output, encoding="utf-8")
    assert result.returncode != 0, output
    assert re.search(r"\[gw\d+\].*PASSED test_healthy.py", output), output
    assert re.search(r"\[gw\d+\].*ERROR test_broken.py", output), output
    assert "50 errors" in output, output
    assert "ORIGINAL_SESSION_SETUP_SENTINEL" in output, output
    assert output.count("First setup failure on ") == 1, output


@pytest.mark.parametrize("when", ["setup", "teardown"])
def test_controller_reports_first_fixture_failure_per_worker(monkeypatch, when):
    from tests.src.e2e import conftest as e2e

    monkeypatch.setattr(e2e, "_doomed_detector", DoomedRunDetector())
    monkeypatch.setattr(e2e, "_reported_failure_workers", set())
    terminal = Mock()
    config = SimpleNamespace(pluginmanager=Mock())
    config.pluginmanager.get_plugin.return_value = terminal

    def report(worker, nodeid, longrepr):
        return SimpleNamespace(
            node=SimpleNamespace(config=config, gateway=SimpleNamespace(id=worker)),
            when=when,
            outcome="failed",
            nodeid=nodeid,
            longrepr=longrepr,
        )

    e2e.pytest_runtest_logreport(report("gw0", "first_test", "original failure"))
    e2e.pytest_runtest_logreport(report("gw0", "second_test", "repeated failure"))
    e2e.pytest_runtest_logreport(report("gw1", "other_test", "other worker failure"))

    assert terminal.write_sep.call_args_list == [
        (("!", f"First {when} failure on gw0: first_test"),),
        (("!", f"First {when} failure on gw1: other_test"),),
    ]
    assert terminal.write_line.call_args_list == [
        (("original failure",),),
        (("other worker failure",),),
    ]
    assert e2e._doomed_detector.streak == 3
