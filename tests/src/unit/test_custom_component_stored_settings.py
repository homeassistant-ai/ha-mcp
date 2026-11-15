"""What the File & YAML Tools entry persists and how it loads it back.

The caller token, the extra directories (#1567) and the extra YAML write keys
(#1887) live in ``.storage``. A store that is corrupt, hand-edited or from
another version must never keep the entry from loading, and never widen what it
may touch. Core sets the entry up from what the store holds, and the tests call
its services the way ha-mcp does.
"""

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import (
    SERVICE_EDIT_YAML_CONFIG,
    SERVICE_GET_ALLOWED_PATHS,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_GET_EXTRA_YAML_KEYS,
    SERVICE_SET_EXTRA_YAML_KEYS,
)
from custom_components.ha_mcp_tools.const import DOMAIN

from ._component_filesystem import (
    ALLOWED_PATHS_STORE,
    CALLER_TOKEN_STORE,
    EXTRA_YAML_KEYS_STORE,
)
from ._file_services import FileService, make_file_service


def _stored(key: str, data: Any, *, version: int = 1) -> dict[str, Any]:
    return {"version": version, "minor_version": 1, "key": key, "data": data}


def _a_store_from_a_newer_version(key: str, data: Any) -> dict[str, Any]:
    """A store Core cannot migrate: loading it raises."""
    return _stored(key, data, version=99)


async def _token(hass: HomeAssistant) -> str:
    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_CALLER_TOKEN, {}, blocking=True, return_response=True
    )
    assert response is not None
    return str(response["token"])


class TestCallerToken:
    async def test_a_stored_token_is_kept(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        hass_storage[CALLER_TOKEN_STORE] = _stored(
            CALLER_TOKEN_STORE, {"token": "existing-tok"}
        )

        await make_file_service(hass)

        assert await _token(hass) == "existing-tok"
        assert hass_storage[CALLER_TOKEN_STORE]["data"] == {"token": "existing-tok"}

    async def test_a_first_run_makes_and_stores_a_token(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        await make_file_service(hass)

        token = await _token(hass)

        assert token
        assert hass_storage[CALLER_TOKEN_STORE]["data"] == {"token": token}

    async def test_a_store_that_cannot_be_loaded_is_replaced_not_fatal(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        """It must not stop the integration from loading; ha-mcp fetches the
        new token on its next call."""
        hass_storage[CALLER_TOKEN_STORE] = _a_store_from_a_newer_version(
            CALLER_TOKEN_STORE, {"token": "existing-tok"}
        )

        await make_file_service(hass)

        token = await _token(hass)
        assert token
        assert token != "existing-tok"
        assert hass_storage[CALLER_TOKEN_STORE]["data"] == {"token": token}


class TestAllowedPathsStore:
    async def test_stored_entries_are_checked_again_and_the_bad_ones_logged(
        self,
        hass: HomeAssistant,
        hass_storage: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid, duplicate and pre-#2875 ``/backup`` entries are dropped, so
        a hand-edited store cannot load a traversal or floor entry."""
        paths = ["pyscript", "../etc", ".storage", "pyscript", 123, "/backup/x"]
        hass_storage[ALLOWED_PATHS_STORE] = _stored(
            ALLOWED_PATHS_STORE, {"paths": paths}
        )

        with caplog.at_level(logging.WARNING):
            files = await make_file_service(hass)

        assert (await files(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == ["pyscript"]
        assert "'/backup/x'" in caplog.text

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(None, id="a first run"),
            pytest.param({"paths": "not-a-list"}, id="paths that are not a list"),
            pytest.param("not-a-mapping", id="data that is not a mapping"),
        ],
    )
    async def test_a_store_that_holds_nothing_usable_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any], stored: Any
    ) -> None:
        if stored is not None:
            hass_storage[ALLOWED_PATHS_STORE] = _stored(ALLOWED_PATHS_STORE, stored)

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == []

    async def test_a_store_that_cannot_be_loaded_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        hass_storage[ALLOWED_PATHS_STORE] = _a_store_from_a_newer_version(
            ALLOWED_PATHS_STORE, {"paths": ["pyscript"]}
        )

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == []


class TestExtraYamlKeysStore:
    async def test_stored_keys_are_checked_again(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        """Whitespace, a denied key, a duplicate and a non-string are dropped;
        the one valid key survives."""
        keys = [" alert2 ", "homeassistant", "alert2", 123]
        hass_storage[EXTRA_YAML_KEYS_STORE] = _stored(
            EXTRA_YAML_KEYS_STORE, {"keys": keys}
        )

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == ["alert2"]

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(None, id="a first run"),
            pytest.param({"keys": "nope"}, id="keys that are not a list"),
        ],
    )
    async def test_a_store_that_holds_nothing_usable_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any], stored: Any
    ) -> None:
        if stored is not None:
            hass_storage[EXTRA_YAML_KEYS_STORE] = _stored(EXTRA_YAML_KEYS_STORE, stored)

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == []

    async def test_a_store_that_cannot_be_loaded_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        hass_storage[EXTRA_YAML_KEYS_STORE] = _a_store_from_a_newer_version(
            EXTRA_YAML_KEYS_STORE, {"keys": ["alert2"]}
        )

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == []


class TestSetExtraYamlKeys:
    @pytest.fixture
    async def files(self, hass: HomeAssistant) -> FileService:
        return await make_file_service(hass)

    @pytest.mark.parametrize(
        ("sent", "kept", "rejected"),
        [
            pytest.param(
                [" beta ", "alpha", "alpha", "beta"],
                ["alpha", "beta"],
                [],
                id="stripped, deduplicated and sorted",
            ),
            pytest.param(
                ["alert2", "homeassistant", "frontend"],
                ["alert2"],
                ["homeassistant", "frontend"],
                id="a key that redefines a trust boundary is dropped",
            ),
            pytest.param(
                ["ok", "   ", ""], ["ok"], ["   ", ""], id="blank entries are dropped"
            ),
            pytest.param(
                ["Alert2", "MQTT"],
                ["Alert2", "MQTT"],
                [],
                id="case is kept, as Home Assistant's own lookup is exact",
            ),
        ],
    )
    async def test_the_keys_are_cleaned_and_the_rest_reported(
        self,
        files: FileService,
        sent: list[str],
        kept: list[str],
        rejected: list[str],
    ) -> None:
        result = await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": sent})

        assert result["success"] is True
        assert result["keys"] == kept
        assert sorted(result["rejected"]) == sorted(rejected)

    async def test_the_keys_are_stored_and_apply_at_once(
        self, files: FileService, hass_storage: dict[str, Any]
    ) -> None:
        await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": [" alert2 ", "alert2"]})

        assert hass_storage[EXTRA_YAML_KEYS_STORE]["data"] == {"keys": ["alert2"]}
        current = await files(SERVICE_GET_EXTRA_YAML_KEYS, {})
        assert current["keys"] == ["alert2"]
        assert "homeassistant" in current["deny_floor"]


class TestEditYamlConfig:
    """``edit_yaml_config`` takes the operator's extra keys from the call and
    from the component's own store (#1887)."""

    @pytest.fixture
    async def files(self, hass: HomeAssistant) -> FileService:
        return await make_file_service(hass)

    @staticmethod
    async def _add(files: FileService, key: str, **fields: Any) -> dict[str, Any]:
        return await files(
            SERVICE_EDIT_YAML_CONFIG,
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": key,
                "content": "a: 1\n",
                **fields,
            },
        )

    async def test_recorder_can_be_edited(self, files: FileService) -> None:
        """recorder is YAML-only, so it is editable in configuration.yaml like
        the other plain keys (#1852)."""
        result = await files(
            SERVICE_EDIT_YAML_CONFIG,
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "recorder",
                "content": "purge_keep_days: 5\n",
            },
        )

        assert result["success"] is True, result

    async def test_a_server_that_still_sends_the_backup_flag_is_not_rejected(
        self, files: FileService
    ) -> None:
        """The component reaches users ahead of the server, and a server older
        than 7.9.0 sends ``backup`` on every call."""
        result = await files(
            SERVICE_EDIT_YAML_CONFIG,
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "recorder",
                "content": "purge_keep_days: 5\n",
                "backup": True,
            },
        )

        assert result["success"] is True, result

    async def test_a_key_outside_the_allowed_list_is_refused_unless_the_call_adds_it(
        self, files: FileService
    ) -> None:
        refused_edit = await self._add(files, "alert2")
        allowed_edit = await self._add(files, "alert2", extra_allowed_keys=["alert2"])

        assert refused_edit["success"] is False
        assert "not in the allowed list" in refused_edit["error"]
        assert allowed_edit["success"] is True, allowed_edit

    async def test_a_stored_key_and_a_key_on_the_call_are_both_allowed(
        self, files: FileService
    ) -> None:
        await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": ["stored1"]})

        stored = await self._add(files, "stored1")
        sent = await self._add(files, "wire1", extra_allowed_keys=["wire1"])
        neither = await self._add(files, "other1")

        assert stored["success"] is True, stored
        assert sent["success"] is True, sent
        assert neither["success"] is False

    async def test_the_deny_floor_cannot_be_lifted_by_the_call_or_the_store(
        self, files: FileService
    ) -> None:
        stored = await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": ["homeassistant"]})

        edit = await self._add(
            files, "homeassistant", extra_allowed_keys=["homeassistant"]
        )

        assert stored["keys"] == []
        assert edit["success"] is False
        assert "can never be edited" in edit["error"]
