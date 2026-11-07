"""Unit tests for ``CleanupSteps`` (``tests/src/e2e/utilities/cleanup.py``).

E2E ``finally`` blocks restore state that later tests on the same worker
share. The cases pin the ways a cleanup can fail without anyone noticing: a
failed restore that is swallowed after a passing body, one that replaces the
body's real failure in pytest's report, or one that skips the restores after
it. Others pin that every failed step is named, that an interrupt propagates
unchanged, that a skipped body counts as passed, that a timeout in a blocking
step lets the next step run, that an ``except`` in the ``finally`` block does
not change how the body's outcome is read, and that a clean cleanup raises
nothing. A cancellation with the timeout hook's ``CANCEL_MESSAGE`` lets the
next step run too, and one that lands in the body leaves none pending for the
steps; any other cancellation ends the cleanup.

No Docker and no HA instance: the steps run plain code.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable

import pytest

from tests.src.e2e.utilities.cleanup import CleanupSteps
from tests.src.timed_out_task import CANCEL_MESSAGE


def _restore_failed() -> None:
    raise RuntimeError("restore failed")


def _raise(error: BaseException) -> None:
    # A call, not a raise statement, so CodeQL does not flag the next step
    # as unreachable: it cannot see that a step suppresses an Exception, and
    # after an interrupt the next step is unreachable on purpose.
    raise error


def _restore_failed_by_wait_helper() -> None:
    # Some wait helpers fail through pytest.fail(), which is a BaseException
    # and not an Exception.
    pytest.fail("restore failed")


def _restore_skipped() -> None:
    pytest.skip("restore failed")


def _finally_with_failed_step(
    body: BaseException | None,
    ran: list[str],
    restore: Callable[[], None] = _restore_failed,
) -> None:
    try:
        if body is not None:
            raise body
    finally:
        with CleanupSteps() as cleanup:
            with cleanup.step():
                ran.append("first")
                restore()
            with cleanup.step():
                ran.append("second")


@pytest.mark.parametrize(
    "restore",
    [
        pytest.param(_restore_failed, id="exception"),
        pytest.param(_restore_failed_by_wait_helper, id="pytest-fail"),
        pytest.param(_restore_skipped, id="pytest-skip"),
    ],
)
def test_failed_step_after_passing_body_fails_the_test(
    restore: Callable[[], None],
) -> None:
    ran: list[str] = []
    # BaseException: a skip escaping the step must fail this test, and
    # pytest.raises(AssertionError) would let it skip the test instead.
    with pytest.raises(BaseException, match="restore failed") as excinfo:
        _finally_with_failed_step(None, ran, restore)
    assert excinfo.type is AssertionError, repr(excinfo.value)
    assert ran == ["first", "second"]
    assert excinfo.value.__cause__ is not None


def test_every_failed_step_is_named_and_the_first_is_the_cause() -> None:
    def body() -> None:
        try:
            pass
        finally:
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    _raise(RuntimeError("first restore failed"))
                with cleanup.step():
                    raise RuntimeError("second restore failed")

    with pytest.raises(AssertionError) as excinfo:
        body()
    assert "first restore failed" in str(excinfo.value)
    assert "second restore failed" in str(excinfo.value)
    assert str(excinfo.value.__cause__) == "first restore failed"


def test_interrupt_in_a_step_propagates_unchanged() -> None:
    interrupt = KeyboardInterrupt()
    ran: list[str] = []

    def body() -> None:
        try:
            pass
        finally:
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    _restore_failed()
                with cleanup.step():
                    _raise(interrupt)
                with cleanup.step():
                    ran.append("after the interrupt")

    with pytest.raises(KeyboardInterrupt) as excinfo:
        body()
    assert excinfo.value is interrupt
    assert ran == []


def test_failed_step_after_failed_body_keeps_the_body_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = ValueError("body failed")
    ran: list[str] = []
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError) as excinfo:
        _finally_with_failed_step(body, ran)
    assert excinfo.value is body
    assert ran == ["first", "second"]
    assert "restore failed" in caplog.text


def test_failed_step_after_skipped_body_fails_the_test() -> None:
    # A skipped test shows only its skip reason in the report, so a failed
    # restore that is only logged would leave no trace there.
    ran: list[str] = []
    with pytest.raises(BaseException, match="restore failed") as excinfo:
        _finally_with_failed_step(pytest.skip.Exception("body skipped"), ran)
    assert excinfo.type is AssertionError, repr(excinfo.value)
    assert ran == ["first", "second"]


def test_timeout_in_a_blocking_step_is_recorded_and_later_steps_still_run() -> None:
    # pytest-timeout's signal method calls pytest.fail() in the frame that
    # runs when the alarm fires: here a blocking step, which records it.
    def on_alarm(signum: int, frame: object) -> None:
        pytest.fail("Timeout (>0.1s) from pytest-timeout.")

    ran: list[str] = []
    previous_handler = signal.signal(signal.SIGALRM, on_alarm)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.1)
    try:
        with pytest.raises(AssertionError, match="Timeout") as excinfo:
            _finally_with_failed_step(None, ran, lambda: time.sleep(5))
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)
    assert ran == ["first", "second"]
    assert isinstance(excinfo.value.__cause__, pytest.fail.Exception)


async def _fail_after_a_tick() -> None:
    await asyncio.sleep(0)
    _restore_failed()


async def _cleanup_with_awaiting_step(ran: list[str]) -> None:
    try:
        pass
    finally:
        with CleanupSteps() as cleanup:
            with cleanup.step():
                ran.append("first")
                await asyncio.sleep(30)
            with cleanup.step():
                # A TaskGroup whose child fails cancels the task. On exit it
                # takes that back and, if task.cancelling() is still nonzero,
                # cancels the task again without a message. Had CleanupSteps not
                # taken back the first step's cancellation, that new one would
                # land at the third step's await and end the cleanup.
                async with asyncio.TaskGroup() as group:
                    group.create_task(_fail_after_a_tick())
            with cleanup.step():
                await asyncio.sleep(0)
                ran.append("third")


async def test_timeout_hook_cancelling_a_step_is_recorded_and_later_steps_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ran: list[str] = []
    task = asyncio.create_task(_cleanup_with_awaiting_step(ran))
    await asyncio.sleep(0)
    task.cancel(CANCEL_MESSAGE)
    with pytest.raises(AssertionError, match="Cleanup failed") as raised:
        # Bounded, so a cleanup left hanging fails the test instead of stalling it.
        await asyncio.wait_for(task, timeout=5)
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert ran == ["first", "third"]
    assert "Cleanup step cancelled after the test timed out" in caplog.text


async def test_timeout_hook_cancelling_the_body_is_taken_back_for_the_steps() -> None:
    ran: list[str] = []

    async def body_then_cleanup() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            with CleanupSteps() as cleanup:
                # See _cleanup_with_awaiting_step: the TaskGroup's cancellation
                # would land at the next step's await.
                with cleanup.step():
                    async with asyncio.TaskGroup() as group:
                        group.create_task(_fail_after_a_tick())
                with cleanup.step():
                    await asyncio.sleep(0)
                    ran.append("second")

    task = asyncio.create_task(body_then_cleanup())
    await asyncio.sleep(0)
    task.cancel(CANCEL_MESSAGE)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert ran == ["second"]


async def test_other_cancellation_of_a_step_ends_the_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ran: list[str] = []
    task = asyncio.create_task(_cleanup_with_awaiting_step(ran))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert ran == ["first"]
    assert "the steps after this one do not run" in caplog.text


def test_step_inside_an_except_does_not_read_as_a_failed_body() -> None:
    # A finally that handles an exception itself: the handled TimeoutError
    # must not make a failed step after a passing body look like cleanup
    # after a failure.
    def body() -> None:
        try:
            pass
        finally:
            with CleanupSteps() as cleanup:
                try:
                    raise TimeoutError
                except TimeoutError:
                    with cleanup.step():
                        _restore_failed()

    with pytest.raises(AssertionError, match="restore failed"):
        body()


def test_passing_steps_after_passing_body_raise_nothing() -> None:
    ran: list[str] = []
    try:
        pass
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            ran.append("restored")
    assert ran == ["restored"]
