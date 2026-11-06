"""Unit tests for ``CleanupSteps`` (``tests/src/e2e/utilities/cleanup.py``).

E2E ``finally`` blocks restore state that later tests on the same worker
share. The cases pin the ways a cleanup can fail without anyone noticing: a
failed restore that is swallowed after a passing body, one that replaces the
body's real failure in pytest's report, or one that skips the restores after
it. Others pin that every failed step is named, that an interrupt propagates
unchanged, that an ``except`` in the ``finally`` block or an ``await`` does
not change how the body's outcome is read, and that a clean cleanup raises
nothing.

No Docker and no HA instance: the steps run plain code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest

from tests.src.e2e.utilities.cleanup import CleanupSteps


def _restore_failed() -> None:
    raise RuntimeError("restore failed")


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
                    raise RuntimeError("first restore failed")
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
                    raise interrupt
                with cleanup.step():
                    ran.append("after the interrupt")

    with pytest.raises(KeyboardInterrupt) as excinfo:
        body()
    assert excinfo.value is interrupt
    assert ran == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(ValueError("body failed"), id="failed"),
        pytest.param(pytest.skip.Exception("body skipped"), id="skipped"),
    ],
)
def test_failed_step_after_failed_body_keeps_the_body_failure(
    body: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[str] = []
    with caplog.at_level(logging.ERROR), pytest.raises(type(body)) as excinfo:
        _finally_with_failed_step(body, ran)
    assert excinfo.value is body
    assert ran == ["first", "second"]
    assert "restore failed" in caplog.text


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


def test_failed_body_in_a_coroutine_is_seen_through_awaits() -> None:
    async def restore() -> None:
        await asyncio.sleep(0)
        _restore_failed()

    async def body() -> None:
        try:
            raise ValueError("body failed")
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await restore()

    with pytest.raises(ValueError, match="body failed"):
        asyncio.run(body())


def test_passing_steps_after_passing_body_raise_nothing() -> None:
    ran: list[str] = []
    try:
        pass
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            ran.append("restored")
    assert ran == ["restored"]
