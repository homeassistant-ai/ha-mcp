"""Wait for Supervisor's existing Core job before requesting another restart."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMockResponse

from ha_mcp.client.rest_client import HomeAssistantClient

from ..e2e import _conftest_topology as topology
from ..e2e._conftest_topology import _wait_for_core_job
from ._supervisor_core import SUPERVISOR, load_supervisor_api, supervisor_requests


@pytest.fixture(autouse=True)
def _haos_runtime_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))


@pytest.mark.parametrize("nested", [False, True])
async def test_core_job_wait_observes_completion(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock,
    monkeypatch,
    nested,
):
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
    load_supervisor_api(hass, aioclient_mock, monkeypatch, {})
    polls = 0

    async def answer_jobs(method, url, data):
        nonlocal polls
        polls += 1
        active["done"] = polls > 1
        return AiohttpClientMockResponse(
            method,
            url,
            status=200,
            text=json.dumps({"result": "ok", "data": {"jobs": [*first, other]}}),
            headers={"Content-Type": "application/json"},
        )

    aioclient_mock.get(f"http://{SUPERVISOR}/jobs/info", side_effect=answer_jobs)
    sleep = Mock()
    monkeypatch.setattr(
        topology, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep)
    )

    await asyncio.to_thread(
        _wait_for_core_job,
        {"base_url": core_client.base_url, "token": core_client.token},
    )

    assert supervisor_requests(aioclient_mock) == [("get", "/jobs/info")] * 2
    sleep.assert_called_once_with(1)


async def test_core_job_wait_is_bounded(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock,
    monkeypatch,
):
    load_supervisor_api(
        hass,
        aioclient_mock,
        monkeypatch,
        {
            ("get", "/jobs/info"): {
                "jobs": [
                    {
                        "name": "home_assistant_core_restart",
                        "done": False,
                        "child_jobs": [],
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(
        topology,
        "time",
        SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 3]), sleep=Mock()),
    )

    with pytest.raises(RuntimeError, match="previous Core job did not finish"):
        await asyncio.to_thread(
            _wait_for_core_job,
            {"base_url": core_client.base_url, "token": core_client.token},
            timeout=2,
        )
    assert supervisor_requests(aioclient_mock) == [("get", "/jobs/info")]
