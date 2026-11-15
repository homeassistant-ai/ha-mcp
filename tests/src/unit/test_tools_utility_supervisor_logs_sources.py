"""The ``slug`` parameter and the error log, beside the Supervisor logs.

``slug`` names an app or a system service, so on any other source the tool
must say it was ignored. ``source="error_log"`` reaches Core's log by three
routes, two of them through Supervisor (``_supervisor_logs_core.py``): directly
inside the app, through Core's ``/api/hassio/core/logs`` proxy on a supervised
install, and as Core's own ``/api/error_log`` file on a container install.
"""

from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ._log_tool_calls import call_kwargs, set_up_core_logs
from ._supervisor_logs_core import Route, Target, ask
from ._supervisor_logs_core import app_route as app_route
from ._supervisor_logs_core import core_proxy as core_proxy
from ._supervisor_logs_core import core_route as core_route
from ._supervisor_logs_core import target as target

LOG_LINE = "2026-11-05 10:00:00.000+00:00 ERROR (MainThread) [a.b.c] boom"


@pytest.fixture
def error_log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves at ``/api/error_log``, as its logging setup names it.

    Core registers the route only when the file is named, so a test takes this
    before ``core_client``.
    """
    log = tmp_path / "home-assistant.log"
    log.write_text(LOG_LINE + "\n", encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(log)
    return log


@pytest.fixture
async def core_logs(hass: HomeAssistant, error_log_file: Path) -> None:
    """Core's ``system_log`` and ``logger``, which the other sources read."""
    await set_up_core_logs(hass)


@pytest.fixture
async def supervised_route(hass: HomeAssistant, core_route: Route) -> Route:
    """``core_route`` on an install whose Core has ``hassio`` loaded."""
    mock_integration(hass, MockModule("hassio"))
    assert await async_setup_component(hass, "hassio", {})
    return core_route


async def read(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    return await LogTools(client).get_logs(**call_kwargs(**overrides))


class TestSlugOnAnotherSource:
    @pytest.mark.usefixtures("core_logs")
    @pytest.mark.parametrize("source", ["system", "error_log", "logger"])
    async def test_a_slug_is_flagged_as_ignored(
        self, source: str, core_client: HomeAssistantClient
    ):
        result = await read(core_client, source=source, slug="core_mosquitto")

        assert any("slug" in w and source in w for w in result.get("warnings", [])), (
            result.get("warnings")
        )

    async def test_a_slug_is_flagged_as_ignored_on_the_logbook(
        self,
        recorder_mock: Recorder,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
    ):
        mock_integration(hass, MockModule("frontend"))
        assert await async_setup_component(hass, "logbook", {})

        result = await read(core_client, source="logbook", slug="core_mosquitto")

        assert any(
            "slug" in w and "logbook" in w for w in result.get("warnings", [])
        ), result.get("warnings")

    async def test_a_slug_on_a_supervisor_log_is_not_flagged(
        self, app_route: Route, target: Target
    ):
        """The twin: the warning must not fire where ``slug`` is the point."""
        app_route.supervisor.reply("line\n")

        result = await ask(app_route.client, target)

        assert not any("slug" in w for w in result.get("warnings", []))


class TestTheErrorLogWindow:
    """Both Supervisor-backed routes read the same window, and only that much.

    An unconditional 20,000-line fetch made ``source='error_log'`` hang for
    15+ minutes on a Supervisor-backed install (#2279).
    """

    @pytest.fixture(params=["app", "supervised"])
    def supervisor_route(self, request: pytest.FixtureRequest) -> Route:
        found: Route = request.getfixturevalue(
            "app_route" if request.param == "app" else "supervised_route"
        )
        return found

    @pytest.mark.parametrize(
        ("limit", "offset", "window"),
        [(100, 0, "entries=:-99:100"), (250, 40, "entries=:-289:250")],
    )
    async def test_the_window_asked_of_supervisor_is_the_one_requested(
        self,
        supervisor_route: Route,
        limit: int,
        offset: int,
        window: str,
    ):
        supervisor_route.supervisor.reply(LOG_LINE + "\n")

        page = await supervisor_route.client.get_error_log(lines=limit, offset=offset)

        (seen,) = supervisor_route.supervisor.requests
        assert seen.path.endswith("/core/logs")
        assert seen.range == window
        assert LOG_LINE in page.text

    @pytest.mark.usefixtures("error_log_file")
    async def test_a_container_install_reads_cores_own_error_log(
        self, core_client: HomeAssistantClient
    ):
        result = await read(core_client, source="error_log")

        assert LOG_LINE in result["log"]
