"""Wait for Supervisor's existing Core job before requesting another restart."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from aiohasupervisor.models.jobs import Job, JobsInfo
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from ha_mcp.client.rest_client import HomeAssistantClient

from ..e2e import _conftest_topology as topology
from ..e2e._conftest_topology import wait_for_core_job
from ._supervisor_core import SUPERVISOR, load_supervisor_api, supervisor_requests

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _job(
    number: int, name: str, *, done: bool, children: list[Job] | None = None
) -> Job:
    return Job(
        name=name,
        reference=None,
        uuid=UUID(int=number),
        progress=0,
        stage=None,
        done=done,
        errors=[],
        created=_CREATED,
        child_jobs=children or [],
        extra=None,
    )


def _jobs_info(*jobs: Job) -> dict[str, Any]:
    return JobsInfo(ignore_conditions=[], jobs=list(jobs)).to_dict()


@pytest.fixture(autouse=True)
def _haos_runtime_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))


@pytest.mark.parametrize("nested", [False, True])
async def test_core_job_wait_observes_completion(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    await load_supervisor_api(hass, aioclient_mock, monkeypatch, {})
    polls = 0

    async def answer_jobs(
        method: str, url: Any, data: Any
    ) -> AiohttpClientMockResponse:
        nonlocal polls
        polls += 1
        restart = _job(1, "home_assistant_core_restart", done=polls > 1)
        first = _job(2, "parent", done=False, children=[restart]) if nested else restart
        other = _job(3, "addon_update", done=False)
        return AiohttpClientMockResponse(
            method,
            url,
            status=200,
            text=json.dumps({"result": "ok", "data": _jobs_info(first, other)}),
            headers={"Content-Type": "application/json"},
        )

    aioclient_mock.get(f"http://{SUPERVISOR}/jobs/info", side_effect=answer_jobs)
    sleep = Mock()
    monkeypatch.setattr(
        topology, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep)
    )

    await asyncio.to_thread(
        wait_for_core_job,
        {"base_url": core_client.base_url, "token": core_client.token},
    )

    assert supervisor_requests(aioclient_mock) == [("get", "/jobs/info")] * 2
    sleep.assert_called_once_with(1)


async def test_core_job_wait_is_bounded(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await load_supervisor_api(
        hass,
        aioclient_mock,
        monkeypatch,
        {
            ("get", "/jobs/info"): _jobs_info(
                _job(1, "home_assistant_core_restart", done=False)
            )
        },
    )
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(
        topology,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep),
    )

    with pytest.raises(
        RuntimeError,
        match=r"previous Core job did not finish.*home_assistant_core_restart",
    ):
        await asyncio.to_thread(
            wait_for_core_job,
            {"base_url": core_client.base_url, "token": core_client.token},
            timeout=2,
        )
    # One poll per second of the two-second budget, then the named failure.
    assert supervisor_requests(aioclient_mock) == [("get", "/jobs/info")] * 2
