"""End a timed-out async test's task before the test ends.

pytest-timeout's ``signal`` method calls ``pytest.fail()`` in whatever frame
runs when it fires. While an async test awaits, that frame is the event loop,
outside the test's task: the test fails, but its task stays pending on the
session-scoped loop. If its await returns, the rest of the test, ``finally``
included, runs while a later test runs the loop; so does the ``finally`` if the
garbage collector collects the task. Otherwise the ``finally`` runs when the
session loop closes.

The hook below cancels a task still pending when the call ends, with
``CANCEL_MESSAGE``, and runs the loop for it to end, so the ``finally`` runs
within the test that timed out. A cancellation lands at the ``await`` the task
is suspended at; blocking code is not interrupted, and the wait cannot end
while it runs. Each cancellation gets ``GRACE_SECONDS``; a task still running
then is cancelled again, at most ``MAX_CANCELLATIONS`` times in all. Each
cancellation the task outlives, a task left pending after the last, and an
error the task ends with are logged as errors. ``CleanupSteps`` counts a step
cancelled with ``CANCEL_MESSAGE`` as a failed step and runs the next one.

Kept out of the conftests so the hook is testable without importing them;
``tests/src/conftest.py`` re-exports ``pytest_runtest_call`` to register it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Generator
from typing import Any

import pytest

logger = logging.getLogger(__name__)

CANCEL_MESSAGE = "cancelled because the test's call ended (tests/src/timed_out_task.py)"
# Fixed rather than the per-test timeout: with the ini timeout of 300s, three
# cancellations could add 15 minutes to one test, the time limit of the shortest
# e2e job. This bounds them to 3 minutes; a cleanup still running after its
# last cancellation is logged and left pending.
GRACE_SECONDS = 60.0
# A bound for a task that swallows cancellation. The first cancellation lands at
# the await the timeout interrupted; each cleanup step that hangs after it uses
# one more.
MAX_CANCELLATIONS = 3


# trylast: run inside the capture plugin's wrapper, so the cleanup's output
# lands in the test's report.
@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    if not isinstance(item, pytest.Function) or not inspect.iscoroutinefunction(
        item.obj
    ):
        return (yield)
    test = item.obj
    task: asyncio.Task[Any] | None = None

    @functools.wraps(test)
    async def record_task(*args: Any, **kwargs: Any) -> Any:
        nonlocal task
        task = asyncio.current_task()
        return await test(*args, **kwargs)

    item.obj = record_task
    try:
        return (yield)
    except KeyboardInterrupt:
        # The session is stopping, and closing its loop cancels the task;
        # ending it here could delay the stop by GRACE_SECONDS per cancellation.
        task = None
        raise
    finally:
        item.obj = test
        if task is not None and not task.done():
            # The call can end before its timeout fires, e.g. on a SystemExit
            # from the loop: stop the timer so it cannot fire during _end_task
            # and replace that failure. The hook is missing with -p no:timeout.
            cancel_timer = getattr(
                item.config.hook, "pytest_timeout_cancel_timer", None
            )
            if cancel_timer is not None:
                cancel_timer(item=item)
            _end_task(task, item.nodeid)


def _end_task(task: asyncio.Task[Any], nodeid: str) -> None:
    loop = task.get_loop()
    for cancellation in range(1, MAX_CANCELLATIONS + 1):
        task.cancel(CANCEL_MESSAGE)
        loop.run_until_complete(asyncio.wait([task], timeout=GRACE_SECONDS))
        if task.done():
            if not task.cancelled() and task.exception() is not None:
                logger.error("%s: task raised", nodeid, exc_info=task.exception())
            return
        logger.error(
            "%s: task still running %ss after cancellation %d of %d",
            nodeid,
            GRACE_SECONDS,
            cancellation,
            MAX_CANCELLATIONS,
        )
    logger.error("%s: task left pending", nodeid)
