"""E2E failures must carry the Home Assistant log that explains them.

A tool call that fails with a webhook 502, or a HACS entry that never loads,
names its cause only in the Home Assistant container log. Without these hooks
that log is lost when the session container is removed.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import docker
import pytest
from docker.models.containers import Container
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from testcontainers.core.container import DockerContainer

from ha_mcp.client.rest_client import HomeAssistantClient

from ..e2e import _conftest_readiness as readiness


class _DockerSide(Container):
    """What Docker holds for the session container: its log, or the error
    reading it."""

    def __init__(self, log: bytes = b"", error: Exception | None = None) -> None:
        super().__init__()
        self.log = log
        self.error = error
        self.log_kwargs: dict[str, Any] = {}

    def logs(self, **kwargs: Any) -> bytes:
        self.log_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.log


class _SessionContainer(DockerContainer):
    """The session's Home Assistant container, without a Docker daemon."""

    def __init__(self, docker_side: _DockerSide) -> None:
        self.docker_side = docker_side

    def get_wrapped_container(self) -> Container:
        return self.docker_side


@pytest.fixture
def item(request: pytest.FixtureRequest) -> Iterator[pytest.Item]:
    """This test's own item, whose config stash holds the container."""
    node = request.node
    assert isinstance(node, pytest.Item)
    yield node
    if readiness.HA_CONTAINER_KEY in node.config.stash:
        del node.config.stash[readiness.HA_CONTAINER_KEY]


def _run_makereport(
    item: pytest.Item, docker_side: _DockerSide | None, *, failed: bool
) -> tuple[pytest.TestReport, pytest.CallInfo[None]]:
    if docker_side is not None:
        item.config.stash[readiness.HA_CONTAINER_KEY] = _SessionContainer(docker_side)
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
    item: pytest.Item,
) -> None:
    docker_side = _DockerSide(b"MCP webhook: upstream request failed: reset\n")

    report, call = _run_makereport(item, docker_side, failed=True)

    assert report.sections == [
        (
            "Home Assistant log during this test",
            "MCP webhook: upstream request failed: reset\n",
        )
    ]
    assert docker_side.log_kwargs["since"] == call.start


@pytest.mark.parametrize(
    ("docker_side", "failed"),
    [
        pytest.param(_DockerSide(b"noise"), False, id="passing-test"),
        pytest.param(None, True, id="haos-lane-has-no-container"),
    ],
)
def test_no_home_assistant_log_is_attached(
    item: pytest.Item, docker_side: _DockerSide | None, failed: bool
) -> None:
    report, _ = _run_makereport(item, docker_side, failed=failed)

    assert report.sections == []


def test_unreadable_log_still_reports_the_failure(item: pytest.Item) -> None:
    docker_side = _DockerSide(error=docker.errors.NotFound("container gone"))

    report, _ = _run_makereport(item, docker_side, failed=True)

    assert "docker logs failed: NotFound" in report.sections[0][1]


async def test_timed_out_entries_gate_shows_the_home_assistant_log_in_the_summary(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "READINESS_TIMINGS",
        "ALL_READINESS_TIMINGS",
        "READINESS_DIAGNOSTICS",
        "ALL_READINESS_DIAGNOSTICS",
    ):
        monkeypatch.setattr(readiness, name, [])
    assert await async_setup_component(hass, "config", {})
    MockConfigEntry(domain="hacs", title="HACS").add_to_hass(hass)
    container = _SessionContainer(
        _DockerSide(b"Setup failed for custom integration 'hacs'\n")
    )

    # Worker side: the gate gives up and the session hands its records over.
    await hass.async_add_executor_job(
        readiness.wait_for_entries_loaded,
        container,
        core_client.base_url,
        {"Authorization": f"Bearer {core_client.token}"},
        0,
    )
    worker = SimpleNamespace(config=SimpleNamespace(workeroutput={}))
    readiness.pytest_sessionfinish(worker, 0)
    monkeypatch.setattr(readiness, "READINESS_DIAGNOSTICS", [])
    monkeypatch.setattr(readiness, "READINESS_TIMINGS", [])

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
    assert "hacs:not_loaded" in summary
    assert "Setup failed for custom integration 'hacs'" in summary
