"""Wait for Supervisor's existing Core job before requesting another restart."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ..e2e import _conftest_topology as topology


@pytest.mark.parametrize("nested", [False, True])
def test_core_job_wait_observes_completion(monkeypatch, nested):
    active = {
        "name": "home_assistant_core_restart",
        "done": False,
        "child_jobs": [],
    }
    other = {"name": "addon_update", "done": False, "child_jobs": []}
    first = (
        [{"name": "parent", "done": False, "child_jobs": [active]}]
        if nested
        else [active]
    )
    command = Mock(
        side_effect=[{"jobs": first}, {"jobs": [{**active, "done": True}, other]}]
    )
    monkeypatch.setattr("haos_runtime._home_assistant_ws_command", command)
    sleep = Mock()
    monkeypatch.setattr(
        topology, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep)
    )

    topology._wait_for_core_job({"base_url": "http://ha.invalid", "token": "test"})

    assert command.call_count == 2
    assert command.call_args.args[2] == {
        "type": "supervisor/api",
        "endpoint": "/jobs/info",
        "method": "get",
    }
    sleep.assert_called_once_with(1)


def test_core_job_wait_is_bounded(monkeypatch):
    command = Mock(
        return_value={
            "jobs": [
                {
                    "name": "home_assistant_core_restart",
                    "done": False,
                    "child_jobs": [],
                }
            ]
        }
    )
    monkeypatch.setattr("haos_runtime._home_assistant_ws_command", command)
    monkeypatch.setattr(
        topology,
        "time",
        SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 3]), sleep=Mock()),
    )

    with pytest.raises(RuntimeError, match="previous Core job did not finish"):
        topology._wait_for_core_job(
            {"base_url": "http://ha.invalid", "token": "test"}, timeout=2
        )
    command.assert_called_once()
