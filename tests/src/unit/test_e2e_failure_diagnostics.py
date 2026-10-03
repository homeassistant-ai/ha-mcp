"""E2E failures must carry the Home Assistant log that explains them.

A tool call that fails with a webhook 502, or a HACS entry that never loads,
names its cause only in the Home Assistant container log. Without these hooks
that log is lost when the session container is removed.
"""

from types import SimpleNamespace
from typing import Any

import docker
import pytest

from ..e2e import _conftest_readiness as readiness


class _FakeContainer:
    def __init__(self, log: bytes = b"", error: Exception | None = None) -> None:
        self.log = log
        self.error = error
        self.log_kwargs: dict[str, Any] = {}

    def get_wrapped_container(self) -> "_FakeContainer":
        return self

    def logs(self, **kwargs: Any) -> bytes:
        self.log_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.log


def _run_makereport(container: _FakeContainer | None, *, failed: bool) -> Any:
    stash = pytest.Stash()
    if container is not None:
        stash[readiness._HA_CONTAINER_KEY] = container
    item = SimpleNamespace(config=SimpleNamespace(stash=stash))
    call = SimpleNamespace(start=1_700_000_000.7)
    report = SimpleNamespace(when="call", failed=failed, sections=[])
    hook = readiness.pytest_runtest_makereport(item, call)
    next(hook)
    with pytest.raises(StopIteration) as done:
        hook.send(report)
    return done.value.value


def test_failed_test_shows_the_home_assistant_log_from_its_own_run() -> None:
    container = _FakeContainer(b"MCP webhook: upstream request failed: reset\n")

    report = _run_makereport(container, failed=True)

    assert report.sections == [
        (
            "Home Assistant log during this test",
            "MCP webhook: upstream request failed: reset\n",
        )
    ]
    assert container.log_kwargs["since"] == 1_700_000_000.7


@pytest.mark.parametrize(
    ("container", "failed"),
    [
        pytest.param(_FakeContainer(b"noise"), False, id="passing-test"),
        pytest.param(None, True, id="haos-lane-has-no-container"),
    ],
)
def test_no_home_assistant_log_is_attached(
    container: _FakeContainer | None, failed: bool
) -> None:
    assert _run_makereport(container, failed=failed).sections == []


def test_unreadable_log_still_reports_the_failure() -> None:
    container = _FakeContainer(error=docker.errors.NotFound("container gone"))

    report = _run_makereport(container, failed=True)

    assert "docker logs failed: NotFound" in report.sections[0][1]


def test_timed_out_entries_gate_shows_the_home_assistant_log_in_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "_READINESS_TIMINGS",
        "_ALL_READINESS_TIMINGS",
        "_READINESS_DIAGNOSTICS",
        "_ALL_READINESS_DIAGNOSTICS",
    ):
        monkeypatch.setattr(readiness, name, [])
    monkeypatch.setattr(
        readiness,
        "_snapshot_config_entries",
        lambda *_args, **_kwargs: (1, 2, True, "hacs:not_loaded"),
    )
    container = _FakeContainer(b"Setup failed for custom integration 'hacs'\n")

    # Worker side: the gate gives up and the session hands its records over.
    readiness._wait_for_entries_loaded(container, "http://ha", {}, timeout=0)
    worker = SimpleNamespace(config=SimpleNamespace(workeroutput={}))
    readiness.pytest_sessionfinish(worker, 0)
    monkeypatch.setattr(readiness, "_READINESS_DIAGNOSTICS", [])
    monkeypatch.setattr(readiness, "_READINESS_TIMINGS", [])

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
