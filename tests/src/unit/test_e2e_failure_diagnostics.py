"""E2E failures must carry the Home Assistant log that explains them.

A tool call that fails with a webhook 502, or a config entry that never loads,
names its cause only in the Home Assistant container log. Without these hooks
that log is lost when the session container is removed.

The session fixture runs for real (``_e2e_session``) against the unit tests'
Home Assistant, so the container the hooks read is the one the fixture keeps
for the session, and the gate that gives up is the fixture's own.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient

from ..e2e import _conftest_readiness as readiness
from ._e2e_session import Session, plain_container_session, running_home_assistant


class _MinuteHand:
    """The readiness gates' clock, half a minute on at every reading: a gate
    with a minute's budget gives up after its second look, without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 30
        return self.now

    def sleep(self, seconds: float) -> None:
        return None


@pytest.fixture
async def running_session(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[Session]:
    """The session fixture, set up and not yet torn down."""
    session = plain_container_session(
        monkeypatch,
        request,
        tmp_path,
        home_assistant_url=core_client.base_url,
        token=core_client.token,
    )
    generator = session.fixture()
    async with running_home_assistant(hass):
        await hass.async_add_executor_job(next, generator)
    try:
        yield session
    finally:
        generator.close()
        await hass.async_add_executor_job(session.request.finalize)


def _makereport(
    request: pytest.FixtureRequest, *, failed: bool
) -> tuple[pytest.TestReport, pytest.CallInfo[None]]:
    """Run the hook on this test's own item, for a call phase that passed or failed."""
    item = request.node
    assert isinstance(item, pytest.Item)
    call = pytest.CallInfo.from_call(lambda: None, when="call")
    report = pytest.TestReport(
        nodeid=item.nodeid,
        location=item.location,
        keywords={},
        outcome="failed" if failed else "passed",
        longrepr=None,
        when="call",
    )
    hook = readiness.pytest_runtest_makereport(item, call)
    next(hook)
    with pytest.raises(StopIteration) as done:
        hook.send(report)
    return done.value.value, call


def test_failed_test_shows_the_home_assistant_log_from_its_own_run(
    running_session: Session, request: pytest.FixtureRequest
) -> None:
    running_session.container.logs.return_value = (
        b"MCP webhook: upstream request failed: reset\n"
    )

    report, call = _makereport(request, failed=True)

    assert report.sections == [
        (
            "Home Assistant log during this test",
            "MCP webhook: upstream request failed: reset\n",
        )
    ]
    assert running_session.container.logs.call_args.kwargs["since"] == call.start


def test_a_passing_test_gets_no_home_assistant_log(
    running_session: Session, request: pytest.FixtureRequest
) -> None:
    running_session.container.logs.return_value = b"noise"

    report, _ = _makereport(request, failed=False)

    assert report.sections == []


def test_a_lane_without_a_session_container_attaches_nothing(
    request: pytest.FixtureRequest,
) -> None:
    """The HAOS lanes boot a VM, not a container, so there is no log to read."""
    report, _ = _makereport(request, failed=True)

    assert report.sections == []


def test_unreadable_log_still_reports_the_failure(
    running_session: Session, request: pytest.FixtureRequest
) -> None:
    running_session.container.logs.side_effect = docker.errors.NotFound(
        "container gone"
    )

    report, _ = _makereport(request, failed=True)

    assert "docker logs failed: NotFound" in report.sections[0][1]


async def test_timed_out_entries_gate_shows_the_home_assistant_log_in_the_summary(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    session = plain_container_session(
        monkeypatch,
        request,
        tmp_path,
        home_assistant_url=core_client.base_url,
        token=core_client.token,
    )
    session.container.logs.return_value = (
        b"Error setting up entry Marketplace for marketplace\n"
    )
    monkeypatch.setattr(readiness, "time", _MinuteHand())
    generator = session.fixture()

    # Worker side: the Marketplace never loads, the gate gives up and the
    # session goes on.
    async with running_home_assistant(hass):
        MockConfigEntry(domain="marketplace", title="Marketplace").add_to_hass(hass)
        await hass.async_add_executor_job(next, generator)
    generator.close()
    await hass.async_add_executor_job(session.request.finalize)
    worker = SimpleNamespace(config=SimpleNamespace(workeroutput={}))
    readiness.pytest_sessionfinish(worker, 0)

    # Controller side: collect the worker's output and render the summary.
    readiness.pytest_testnodedown(
        SimpleNamespace(workeroutput=worker.config.workeroutput), None
    )
    lines: list[str] = []
    terminal = SimpleNamespace(
        section=lambda title: lines.append(f"== {title}"),
        write_line=lines.append,
    )
    readiness.pytest_terminal_summary(terminal, 0, None)

    summary = "\n".join(lines)
    assert "marketplace:not_loaded" in summary
    assert "Error setting up entry Marketplace for marketplace" in summary
