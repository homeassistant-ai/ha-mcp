"""``fetch_integration_diagnostics`` on Core's own diagnostics download.

Core's ``diagnostics`` integration serves ``/api/diagnostics/config_entry/...``
for a test integration whose diagnostics platform returns ``payload``, so the
dump, its top-level keys and every refusal (404, 403, 401, 500) are Core's.
The helper's projection (``fields``), walk (``data_path``), pagination and byte
cap run on that dump.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from ipaddress import ip_address
from typing import Any, NamedTuple
from unittest.mock import Mock, patch

import pytest
from homeassistant.components.http.ban import KEY_BAN_MANAGER
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import NON_ADMIN_TOKEN_WARNING, HomeAssistantClient
from ha_mcp.tools.diagnostics_helpers import fetch_integration_diagnostics

DOMAIN = "diagnosed"


class Diagnosed(NamedTuple):
    """The test integration's entry, what its platform returns, and a gate
    its platform waits on before answering."""

    entry: MockConfigEntry
    payload: dict[str, Any]
    answer: asyncio.Event
    failure: list[Exception]


@pytest.fixture
async def diagnosed(hass: HomeAssistant, core_client: HomeAssistantClient) -> Diagnosed:
    """An integration with a diagnostics platform, set up beside Core's
    ``diagnostics``; its config-entry and device dumps return ``payload``."""
    payload: dict[str, Any] = {}
    failure: list[Exception] = []
    answer = asyncio.Event()
    answer.set()

    async def entry_dump(core: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
        await answer.wait()
        if failure:
            raise failure[0]
        return payload

    async def device_dump(
        core: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
    ) -> dict[str, Any]:
        return payload

    mock_integration(hass, MockModule(DOMAIN))
    mock_platform(
        hass,
        f"{DOMAIN}.diagnostics",
        Mock(
            async_get_config_entry_diagnostics=entry_dump,
            async_get_device_diagnostics=device_dump,
        ),
    )
    assert await async_setup_component(hass, DOMAIN, {})
    assert await async_setup_component(hass, "diagnostics", {})
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return Diagnosed(entry, payload, answer, failure)


@pytest.fixture
async def invalid_token_client(
    core_client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantClient]:
    client = HomeAssistantClient(
        base_url=core_client.base_url, token="not-a-token", verify_ssl=False
    )
    try:
        yield client
    finally:
        await client.close()


async def _fetch(
    client: HomeAssistantClient, diagnosed: Diagnosed, **kwargs: Any
) -> dict[str, Any]:
    return await fetch_integration_diagnostics(
        client, diagnosed.entry.entry_id, **kwargs
    )


class TestFetch:
    async def test_returns_cores_dump_of_the_entry(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["bridge"] = {"ip": "10.0.0.5"}

        result = await _fetch(core_client, diagnosed)

        assert set(result) == {"config_entry_id", "data"}
        assert result["config_entry_id"] == diagnosed.entry.entry_id
        assert result["data"]["data"] == {"bridge": {"ip": "10.0.0.5"}}
        assert result["data"]["integration_manifest"]["domain"] == DOMAIN

    async def test_device_scope_returns_the_device_dump(
        self,
        core_client: HomeAssistantClient,
        diagnosed: Diagnosed,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        diagnosed.payload["device"] = "info"
        device = device_registry.async_get_or_create(
            config_entry_id=diagnosed.entry.entry_id, identifiers={(DOMAIN, "dev")}
        )

        result = await _fetch(core_client, diagnosed, device_id=device.id)

        assert result["device_id"] == device.id
        assert result["data"]["data"] == {"device": "info"}
        assert "error" not in result

    @pytest.mark.parametrize("entry_id", ["", None])
    async def test_missing_entry_id_asks_for_it_without_a_request(
        self, core_client: HomeAssistantClient, entry_id: str | None
    ) -> None:
        """The echo keeps what the caller gave: "" and None stay distinct."""
        with patch.object(
            core_client, "guarded_request", wraps=core_client.guarded_request
        ) as sent:
            result = await fetch_integration_diagnostics(core_client, entry_id)

        assert result["config_entry_id"] == entry_id
        assert "config_entry_id is required" in result["error"]
        assert "ha_get_integration" in result["error"]
        sent.assert_not_awaited()


class TestRefusals:
    async def test_unknown_entry_is_unavailable(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        result = await fetch_integration_diagnostics(core_client, "missing_entry")

        assert result["config_entry_id"] == "missing_entry"
        assert "Diagnostics not available" in result["error"]
        assert "config entry" in result["error"]
        assert "ha_get_integration" in result["error"]

    async def test_unknown_device_names_the_device(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        result = await _fetch(core_client, diagnosed, device_id="dev_xyz")

        assert "Diagnostics not available for this device" in result["error"]

    async def test_banned_host_names_proxy_or_ip_ban(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core's http.ban answers a banned address with 403."""
        await hass.http.app[KEY_BAN_MANAGER].async_add_ban(ip_address("127.0.0.1"))

        result = await fetch_integration_diagnostics(core_client, "entry_abc")

        assert "HTTP 403" in result["error"]
        assert "http.ban" in result["error"]

    # Core closes the socket whose token it refused; the client's close reply
    # re-arms aiohttp's heartbeat timer on that closed server socket, which
    # nothing cancels (as in test_dashboard_write_not_sent.py).
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    async def test_rejected_token_names_token_validity(
        self, invalid_token_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """Core answers 401 for an invalid token, and for a non-admin one past
        an inconclusive admin probe (``@require_admin``)."""
        result = await _fetch(invalid_token_client, diagnosed)

        assert "HTTP 401" in result["error"]
        assert "invalid or expired" in result["error"]
        assert "non-admin" in result["error"]
        assert "long-lived access token" in result["error"]

    async def test_non_admin_token_names_the_unsupported_token(
        self,
        hass: HomeAssistant,
        read_only_client: HomeAssistantClient,
        diagnosed: Diagnosed,
    ) -> None:
        """Core's ``auth/current_user`` says the user is no admin, so the
        request is refused unsent."""
        assert await async_setup_component(hass, "auth", {})

        result = await _fetch(read_only_client, diagnosed)

        assert "is admin-only" in result["error"]
        assert NON_ADMIN_TOKEN_WARNING in result["error"]

    async def test_slow_dump_times_out_after_the_given_timeout(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        diagnosed: Diagnosed,
    ) -> None:
        """The caller's timeout reaches the request and is named in the error."""
        diagnosed.answer.clear()
        try:
            result = await _fetch(core_client, diagnosed, timeout_seconds=0.5)
        finally:
            diagnosed.answer.set()
            await hass.async_block_till_done()

        assert "timed out after 0.5s" in result["error"]
        assert "ZHA" in result["error"]

    async def test_refused_connection_is_not_a_timeout(
        self, unreachable_client: HomeAssistantClient
    ) -> None:
        result = await fetch_integration_diagnostics(unreachable_client, "entry_abc")

        assert "connection failed" in result["error"]
        assert "timed out" not in result["error"]

    async def test_server_error_names_the_status(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """Core answers 500 when the integration's dump fails."""
        diagnosed.failure.append(RuntimeError("dump failed"))

        result = await _fetch(core_client, diagnosed)

        assert "HTTP 500" in result["error"]


class TestFieldsAndCap:
    async def test_fields_keep_the_requested_top_level_keys(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        full = await _fetch(core_client, diagnosed)

        result = await _fetch(core_client, diagnosed, fields=["home_assistant", "data"])

        assert result["data"] == {
            "home_assistant": full["data"]["home_assistant"],
            "data": {},
        }
        assert "omitted_fields" not in result

    async def test_unknown_fields_are_listed_once_as_omitted(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        result = await _fetch(
            core_client,
            diagnosed,
            fields=["data", "made_up_key", "made_up_key", "another_missing"],
        )

        assert set(result["data"]) == {"data"}
        assert result["omitted_fields"] == ["made_up_key", "another_missing"]

    async def test_all_fields_missing_leaves_an_empty_dict(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """No silent fall-through to the full payload."""
        result = await _fetch(core_client, diagnosed, fields=["foo", "bar", "baz"])

        assert result["data"] == {}
        assert result["omitted_fields"] == ["foo", "bar", "baz"]

    async def test_fields_are_not_applied_to_an_error(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        result = await fetch_integration_diagnostics(
            core_client, "missing_entry", fields=["home_assistant", "issues"]
        )

        assert "error" in result
        assert "data" not in result
        assert "omitted_fields" not in result

    async def test_oversize_dump_is_dropped_with_its_keys(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["blob"] = "x" * 5000
        full = await _fetch(core_client, diagnosed)

        result = await _fetch(core_client, diagnosed, truncate_at_bytes=200)

        assert result["truncated"] is True
        assert result["byte_cap"] == 200
        assert result["bytes_total"] > 200
        assert result["available_fields"] == sorted(full["data"])
        assert "data" not in result

    async def test_dump_under_the_cap_is_kept(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        result = await _fetch(core_client, diagnosed, truncate_at_bytes=1_000_000)

        assert result["data"]["data"] == {}
        assert "truncated" not in result
        assert "bytes_total" not in result

    async def test_cap_measures_the_projected_dump(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["blob"] = "x" * 5000

        result = await _fetch(
            core_client, diagnosed, fields=["issues"], truncate_at_bytes=200
        )

        assert "truncated" not in result
        assert result["data"] == {"issues": []}

    async def test_capped_list_lists_no_fields(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """``available_fields`` names a dict's keys; a list has none."""
        diagnosed.payload["devices"] = ["x" * 1000 for _ in range(50)]

        result = await _fetch(
            core_client, diagnosed, data_path="data.devices", truncate_at_bytes=200
        )

        assert result["truncated"] is True
        assert result["byte_cap"] == 200
        assert result["bytes_total"] > 200
        assert "available_fields" not in result
        assert "data" not in result


class TestDataPath:
    async def test_walks_into_a_sub_tree(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["bridge"] = {"ip": "10.0.0.5"}

        result = await _fetch(core_client, diagnosed, data_path="data.bridge")

        assert result["data"] == {"ip": "10.0.0.5"}
        assert result["data_path"] == "data.bridge"

    async def test_walks_to_a_scalar(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["bridge"] = {"ip": "10.0.0.5"}

        result = await _fetch(core_client, diagnosed, data_path="data.bridge.ip")

        assert result["data"] == "10.0.0.5"
        assert result["data_path"] == "data.bridge.ip"

    async def test_list_without_limit_is_returned_whole(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["devices"] = [{"id": i} for i in range(5)]

        result = await _fetch(core_client, diagnosed, data_path="data.devices")

        assert result["data"] == [{"id": i} for i in range(5)]
        assert result["data_path"] == "data.devices"

    async def test_limit_returns_a_page_envelope(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["devices"] = [{"id": i} for i in range(25)]

        result = await _fetch(
            core_client, diagnosed, data_path="data.devices", data_limit=10
        )

        assert result["data"] == {
            "path": "data.devices",
            "items": [{"id": i} for i in range(10)],
            "offset": 0,
            "limit": 10,
            "total": 25,
            "has_more": True,
        }
        assert result["data_path"] == "data.devices"

    @pytest.mark.parametrize(
        ("offset", "limit", "items"),
        [(20, 10, ["20", "21", "22", "23", "24"]), (30, 3, [])],
        ids=["last_page", "past_the_end"],
    )
    async def test_offset_pages_to_the_end(
        self,
        core_client: HomeAssistantClient,
        diagnosed: Diagnosed,
        offset: int,
        limit: int,
        items: list[str],
    ) -> None:
        """The slice never wraps or errors past the end."""
        diagnosed.payload["items"] = [str(i) for i in range(25)]

        result = await _fetch(
            core_client,
            diagnosed,
            data_path="data.items",
            data_offset=offset,
            data_limit=limit,
        )

        page = result["data"]
        assert page["items"] == items
        assert (page["offset"], page["limit"], page["total"]) == (offset, limit, 25)
        assert page["has_more"] is False

    async def test_offset_without_limit_is_ignored_with_a_warning(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        diagnosed.payload["items"] = [{"id": i} for i in range(5)]

        result = await _fetch(
            core_client, diagnosed, data_path="data.items", data_offset=2
        )

        assert result["data"] == [{"id": i} for i in range(5)]
        assert "data_offset ignored" in result["data_pagination_warning"]
        assert "data_limit not set" in result["data_pagination_warning"]

    async def test_limit_on_a_dict_is_ignored_with_a_warning(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """The raw value must not read as ``page 1 of N``."""
        diagnosed.payload["bridge"] = {"ip": "10.0.0.5"}

        result = await _fetch(
            core_client, diagnosed, data_path="data.bridge", data_limit=10
        )

        assert result["data"] == {"ip": "10.0.0.5"}
        assert "not a list" in result["data_pagination_warning"]
        assert "dict" in result["data_pagination_warning"]

    async def test_offset_without_path_is_ignored_with_a_warning(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        result = await _fetch(core_client, diagnosed, data_offset=5)

        assert "data_path not set" in result["data_pagination_warning"]
        assert result["data"]["data"] == {}

    @pytest.mark.parametrize("path", ["", "   "])
    async def test_blank_path_is_unset_with_a_warning(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed, path: str
    ) -> None:
        """A blank path is not applied, and the caller is told so."""
        result = await _fetch(core_client, diagnosed, data_path=path)

        assert result["data"]["data"] == {}
        assert "data_path" not in result
        assert "data_path_error" not in result
        assert "empty or whitespace-only" in result["data_pagination_warning"]

    async def test_blank_path_warning_wins_over_the_orphan_offset_one(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """The blank path is the caller's problem; the dropped offset follows."""
        result = await _fetch(core_client, diagnosed, data_path="   ", data_offset=5)

        assert "empty or whitespace-only" in result["data_pagination_warning"]
        assert "data_path not set" not in result["data_pagination_warning"]

    async def test_path_through_a_projected_out_key_fails(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """``fields`` runs first, so the path walks the projected dump."""
        result = await _fetch(
            core_client, diagnosed, fields=["home_assistant"], data_path="data.devices"
        )

        assert result["data"] is None
        assert "missing key 'data'" in result["data_path_error"]

    async def test_capped_page_keeps_its_envelope(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """The cap measures the page's ``items`` and drops only them, so the
        caller can ask for a narrower page."""
        diagnosed.payload["items"] = ["x" * 200 for _ in range(20)]

        result = await _fetch(
            core_client,
            diagnosed,
            data_path="data.items",
            data_limit=10,
            truncate_at_bytes=500,
        )

        assert result["truncated"] is True
        assert result["byte_cap"] == 500
        assert result["data_path"] == "data.items"
        assert result["data"] == {
            "path": "data.items",
            "offset": 0,
            "limit": 10,
            "total": 20,
            "has_more": True,
            "truncated": True,
        }


class TestDataPathErrors:
    async def test_missing_key_lists_what_is_there(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        full = await _fetch(core_client, diagnosed)

        result = await _fetch(core_client, diagnosed, data_path="missing")

        assert result["data"] is None
        assert "missing key 'missing'" in result["data_path_error"]
        assert str(sorted(full["data"])) in result["data_path_error"]

    @pytest.mark.parametrize(
        ("path", "error"),
        [
            ("data.devices.0", "cannot descend into list"),
            ("data.bridge.ip.octet", "cannot descend into str"),
            ("data..bridge", "empty segment"),
        ],
        ids=["list", "str", "empty_segment"],
    )
    async def test_unwalkable_path_names_why(
        self,
        core_client: HomeAssistantClient,
        diagnosed: Diagnosed,
        path: str,
        error: str,
    ) -> None:
        diagnosed.payload.update(devices=["d1", "d2"], bridge={"ip": "10.0.0.5"})

        result = await _fetch(core_client, diagnosed, data_path=path)

        assert result["data"] is None
        assert error in result["data_path_error"]

    async def test_null_sub_tree_has_its_own_message(
        self, core_client: HomeAssistantClient, diagnosed: Diagnosed
    ) -> None:
        """Not the generic ``cannot descend into NoneType``."""
        diagnosed.payload["version"] = None

        result = await _fetch(core_client, diagnosed, data_path="data.version.minor")

        assert result["data"] is None
        assert "resolved to null" in result["data_path_error"]
        assert "sub-tree not present" in result["data_path_error"]

    @pytest.mark.parametrize(
        ("dump", "path", "hinted"),
        [
            ({"version": "1", "build": "abc"}, "data.versionz", False),
            (
                {"sensor.zha_temp_42": 21.5, "binary_sensor.door": True},
                "data.sensor",
                True,
            ),
        ],
        ids=["plain_typo", "dotted_sibling"],
    )
    async def test_dotted_key_hint_only_beside_a_dotted_sibling(
        self,
        core_client: HomeAssistantClient,
        diagnosed: Diagnosed,
        dump: dict[str, Any],
        path: str,
        hinted: bool,
    ) -> None:
        """A plain typo is not told its '.' is mis-parsed; a sibling key with a
        literal '.' explains why a plausible path fails."""
        diagnosed.payload.update(dump)

        result = await _fetch(core_client, diagnosed, data_path=path)

        error = result["data_path_error"]
        assert f"missing key '{path.rsplit('.', 1)[1]}'" in error
        assert ("literal '.'" in error) is hinted
        assert ("not addressable" in error) is hinted
