"""Tests for ``tests/src/timed_out_task.py``: a timed-out async test's task ends
within that test, so its ``finally`` does not run while a later test runs."""

import re
import textwrap
from pathlib import Path

import pytest

from tests.src import timed_out_task

from ._pytest_subprocess import run_pytest


def _run(
    tmp_path: Path,
    tests: str,
    *,
    timeout_plugin: bool = True,
    grace_seconds: float = 0.5,
) -> tuple[str, list[str]]:
    # Mirrors the asyncio and timeout settings in pyproject.toml, with 0.5s for
    # the timeout and, by default, for each cancellation to keep the run fast.
    (tmp_path / "pytest.ini").write_text(
        textwrap.dedent(
            """\
            [pytest]
            asyncio_mode = auto
            asyncio_default_fixture_loop_scope = session
            asyncio_default_test_loop_scope = session
            timeout = 0.5
            timeout_method = signal
            timeout_func_only = true
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f"""\
            from tests.src import timed_out_task
            from tests.src.timed_out_task import pytest_runtest_call

            timed_out_task.GRACE_SECONDS = {grace_seconds}
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "test_order.py").write_text(
        textwrap.dedent(
            """\
            import asyncio
            from pathlib import Path

            import pytest

            def record(event):
                with Path("order.txt").open("a") as order:
                    order.write(event + "\\n")
            """
        )
        + textwrap.dedent(tests),
        encoding="utf-8",
    )
    result = run_pytest(
        tmp_path,
        "-p",
        "pytest_asyncio.plugin",
        *(["-p", "pytest_timeout"] if timeout_plugin else []),
        "-p",
        "no:cacheprovider",
        "-rA",
        # Wide enough that the short test summary keeps the failure reasons whole.
        env={"COLUMNS": "200"},
    )
    output = result.stdout + result.stderr
    order_file = tmp_path / "order.txt"
    order = (
        order_file.read_text(encoding="utf-8").split() if order_file.exists() else []
    )
    return output, order


def test_suite_conftest_registers_the_hook(pytestconfig: pytest.Config) -> None:
    impls = pytestconfig.pluginmanager.hook.pytest_runtest_call.get_hookimpls()
    assert any(impl.function is timed_out_task.pytest_runtest_call for impl in impls)


def test_timed_out_test_finishes_its_cleanup_before_the_next_test_starts(tmp_path):
    output, order = _run(
        tmp_path,
        """\
        async def test_a_times_out():
            try:
                await asyncio.sleep(30)
            finally:
                record("a-cleanup-start")
                await asyncio.sleep(0)
                print("a-cleanup-output")
                record("a-cleanup-done")

        async def test_b_runs_after(tmp_path):
            record("b-start")
        """,
    )
    assert "FAILED test_order.py::test_a_times_out - Failed: Timeout" in output, output
    assert "PASSED test_order.py::test_b_runs_after" in output, output
    assert order == ["a-cleanup-start", "a-cleanup-done", "b-start"], output
    assert "ERROR    tests.src.timed_out_task" not in output, output
    # The cleanup's output lands in the timed-out test's report.
    assert re.search(r"Captured stdout call -+\na-cleanup-output", output), output


def test_hook_logs_nothing_for_a_task_that_ends_without_an_error(tmp_path):
    output, _ = _run(
        tmp_path,
        """\
        async def test_a_fails_without_timing_out():
            assert 1 == 2

        async def test_b_times_out_and_returns_on_cancellation():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                pass
        """,
    )
    assert "FAILED test_order.py::test_a_fails_without_timing_out" in output, output
    assert (
        "FAILED test_order.py::test_b_times_out_and_returns_on_cancellation"
        " - Failed: Timeout" in output
    ), output
    assert "ERROR    tests.src.timed_out_task" not in output, output


def test_cleanup_step_cancelled_by_the_hook_lets_the_next_step_run(tmp_path):
    output, order = _run(
        tmp_path,
        """\
        from tests.src.e2e.utilities.cleanup import CleanupSteps

        async def test_a_times_out_and_its_first_step_hangs():
            try:
                await asyncio.sleep(30)
            finally:
                with CleanupSteps() as cleanup:
                    with cleanup.step():
                        await asyncio.sleep(30)
                    with cleanup.step():
                        record("second-step")
        """,
    )
    assert "task still running 0.5s after cancellation 1 of" in output, output
    assert order == ["second-step"], output


def test_interrupt_stops_the_session_without_the_hook_ending_the_task(tmp_path):
    output, order = _run(
        tmp_path,
        f"""\
        def interrupt():
            raise KeyboardInterrupt

        # No pytest-timeout limit in play: the interrupt alone ends the test.
        @pytest.mark.timeout(30)
        async def test_a_is_interrupted():
            # KeyboardInterrupt from the event loop while the task is pending,
            # as a Ctrl-C can raise it. Raised directly, because pytest-asyncio
            # versions differ in how they handle SIGINT.
            asyncio.get_running_loop().call_soon(interrupt)
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError as error:
                hook = error.args == ({timed_out_task.CANCEL_MESSAGE!r},)
                record("hook-cancel" if hook else "other-cancel")

        async def test_b_does_not_run():
            record("b-start")
        """,
    )
    assert "KeyboardInterrupt" in output, output
    assert order == ["other-cancel"], output


def test_system_exit_from_the_loop_does_not_leave_the_task_pending(tmp_path):
    # pytest reports a SystemExit as a failed test and goes on, unlike a
    # KeyboardInterrupt, so the hook must still end the task. Run without
    # pytest-timeout, whose hook the task's end calls when it is loaded.
    output, order = _run(
        tmp_path,
        """\
        import sys

        async def test_a_exits_from_the_loop():
            asyncio.get_running_loop().call_soon(sys.exit, 3)
            try:
                await asyncio.sleep(30)
            finally:
                record("a-cleanup")

        async def test_b_runs_after():
            record("b-start")
        """,
        timeout_plugin=False,
    )
    assert "FAILED test_order.py::test_a_exits_from_the_loop - SystemExit" in output, (
        output
    )
    assert order == ["a-cleanup", "b-start"], output


def test_timeout_does_not_fire_while_the_hook_ends_the_task(tmp_path):
    output, order = _run(
        tmp_path,
        """\
        import sys

        async def test_a_exits_from_the_loop():
            # Exits on the loop's next pass, long before the timeout.
            asyncio.get_running_loop().call_soon(sys.exit, 3)
            try:
                await asyncio.sleep(30)
            finally:
                record("a-cleanup-start")
                # Past the call's 0.5s timeout.
                await asyncio.sleep(0.6)
                record("a-cleanup-done")

        async def test_b_runs_after():
            record("b-start")
        """,
        # Keeps the 0.6s cleanup within the hook's first cancellation.
        grace_seconds=5,
    )
    assert "FAILED test_order.py::test_a_exits_from_the_loop - SystemExit" in output, (
        output
    )
    assert "Failed: Timeout" not in output, output
    assert order == ["a-cleanup-start", "a-cleanup-done", "b-start"], output


def test_hung_cleanup_is_cancelled_and_logged_before_the_next_test(tmp_path):
    output, order = _run(
        tmp_path,
        """\
        async def test_a_times_out_and_its_cleanup_hangs():
            try:
                await asyncio.sleep(30)
            finally:
                record("a-cleanup-start")
                # Longer than a cancellation's 0.5s, shorter than test b, so a
                # cleanup left running would finish during b.
                await asyncio.sleep(1)
                record("a-cleanup-done")

        @pytest.mark.timeout(5)
        async def test_b_runs_after():
            record("b-start")
            await asyncio.sleep(1.5)
            record("b-end")
        """,
    )
    assert "PASSED test_order.py::test_b_runs_after" in output, output
    assert "task still running 0.5s after cancellation 1 of" in output, output
    assert order == ["a-cleanup-start", "b-start", "b-end"], output


def test_cleanup_error_after_a_timeout_is_logged(tmp_path):
    output, _ = _run(
        tmp_path,
        """\
        async def test_a_times_out_and_its_cleanup_raises():
            try:
                await asyncio.sleep(30)
            finally:
                raise RuntimeError("cleanup-" + "error")
        """,
    )
    assert "test_a_times_out_and_its_cleanup_raises: task raised" in output, output
    assert "RuntimeError: cleanup-error" in output, output


def test_task_that_swallows_cancellation_is_left_pending_after_the_limit(tmp_path):
    output, order = _run(
        tmp_path,
        f"""\
        async def test_a_times_out_and_swallows_cancellation():
            try:
                await asyncio.sleep(30)
            finally:
                # The hook's first cancellation lands in the body, so this
                # swallows its others plus the one from the session loop
                # closing after test b.
                for _ in range({timed_out_task.MAX_CANCELLATIONS}):
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        record("swallowed")

        async def test_b_runs_after():
            record("b-start")
        """,
    )
    assert "PASSED test_order.py::test_b_runs_after" in output, output
    assert "test_a_times_out_and_swallows_cancellation: task left pending" in output
    assert order == ["swallowed"] * (timed_out_task.MAX_CANCELLATIONS - 1) + [
        "b-start",
        "swallowed",
    ], output
