"""The custom directories editor on Home Assistant (#1567).

``/api/settings/fs-custom-paths`` reads and replaces the extra directories of
the File & YAML Tools entry through its services; the entry, not the page,
decides what is allowed. The server's client talks to a real ``hass`` with
the entry set up as ``_file_services`` sets it up.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings

from ._file_services import make_file_service
from ._settings_ui_app import app_server, settings_http
from ._settings_ui_app import settings_world as settings_world

PATHS = "/api/settings/fs-custom-paths"
UNREACHABLE = "Could not reach the ha_mcp_tools component:"


@pytest.fixture
def file_tools_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "true")
    reset_global_settings()


@pytest.fixture
async def tools_entry(hass: HomeAssistant) -> None:
    await make_file_service(hass, allowed="pyscript")


async def _get(client: HomeAssistantClient) -> dict[str, Any]:
    async with settings_http(app_server(client=client)) as http:
        response = await http.get(PATHS)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def _save(client: HomeAssistantClient, **kwargs: Any) -> httpx.Response:
    async with settings_http(app_server(client=client)) as http:
        return await http.post(PATHS, **kwargs)


async def test_with_file_tools_off_the_editor_says_so_without_asking(
    core_client: HomeAssistantClient,
) -> None:
    body = await _get(core_client)

    assert body["available"] is False
    assert "disabled" in body["reason"].lower()


async def test_with_file_tools_off_a_save_is_refused(
    core_client: HomeAssistantClient,
) -> None:
    response = await _save(core_client, json={"paths": ["pyscript"]})

    assert response.status_code == 409


@pytest.mark.usefixtures("file_tools_on", "tools_entry")
class TestEntrySetUp:
    async def test_the_editor_shows_the_directories_and_the_deny_floor(
        self, core_client: HomeAssistantClient
    ) -> None:
        body = await _get(core_client)

        assert body["available"] is True
        assert body["paths"] == ["pyscript"]
        assert ".storage" in body["deny_floor"]

    async def test_a_save_reports_what_the_entry_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        """The entry drops the deny-floor directory and applies the rest live."""
        response = await _save(core_client, json={"paths": ["pyscript", ".storage"]})

        body = response.json()
        assert body["success"] is True
        assert body["paths"] == ["pyscript"]
        assert body["rejected"] == [".storage"]
        assert body["restart_required"] is False

    async def test_paths_that_are_not_a_list_are_a_400(
        self, core_client: HomeAssistantClient
    ) -> None:
        response = await _save(core_client, json={"paths": "not-a-list"})

        assert response.status_code == 400

    async def test_a_body_that_is_not_json_is_a_400(
        self, core_client: HomeAssistantClient
    ) -> None:
        response = await _save(core_client, content=b"not json")

        assert response.status_code == 400

    async def test_the_sidecar_asks_with_a_client_of_its_own_and_closes_it(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The stdio sidecar has no server: it builds a client from the URL and
        token it inherits, and must not leave it open."""
        monkeypatch.setenv("HOMEASSISTANT_URL", core_client.base_url)
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", core_client.token)
        reset_global_settings()
        closed: list[HomeAssistantClient] = []
        close = HomeAssistantClient.close

        async def recording_close(client: HomeAssistantClient) -> None:
            closed.append(client)
            await close(client)

        monkeypatch.setattr(HomeAssistantClient, "close", recording_close)

        async with settings_http(is_sidecar=True) as http:
            response = await http.get(PATHS)

        assert response.json()["available"] is True
        assert len(closed) == 1
        assert closed[0] is not core_client


@pytest.mark.usefixtures("file_tools_on")
class TestEntryNotSetUp:
    """#1996: the entry's own reason, with its first suggestion, instead of a
    "could not reach" prefix on a raw JSON error."""

    async def test_the_editor_says_what_to_set_up(
        self, core_client: HomeAssistantClient
    ) -> None:
        body = await _get(core_client)

        assert body["available"] is False
        assert UNREACHABLE not in body["reason"]
        assert not body["reason"].startswith("{")

    async def test_a_save_says_what_to_set_up(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A user fixing the directories while the entry is missing saves."""
        response = await _save(core_client, json={"paths": ["pyscript"]})

        assert response.status_code < 500
        message = response.json()["error"]["message"]
        assert UNREACHABLE not in message
        assert not message.startswith("{")


@pytest.mark.usefixtures("file_tools_on")
class TestHomeAssistantUnreachable:
    async def test_the_editor_says_it_could_not_reach_the_component(
        self, unreachable_client: HomeAssistantClient
    ) -> None:
        """An unavailable editor, never a 500."""
        body = await _get(unreachable_client)

        assert body["available"] is False
        assert body["reason"].startswith(UNREACHABLE)

    async def test_a_save_is_a_502(
        self, unreachable_client: HomeAssistantClient
    ) -> None:
        response = await _save(unreachable_client, json={"paths": ["pyscript"]})

        assert response.status_code == 502
        assert response.json()["error"]["message"].startswith(UNREACHABLE)
