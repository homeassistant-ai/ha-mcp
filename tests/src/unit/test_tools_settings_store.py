"""Distinguish a missing File & YAML store from one Core could not persist."""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.ha_mcp_tools import (
    SERVICE_GET_EXTRA_YAML_KEYS,
    SERVICE_SET_EXTRA_YAML_KEYS,
    settings_transfer,
)
from custom_components.ha_mcp_tools.tools_settings_store import (
    ALLOWED_PATHS_KEY,
    EXTRA_YAML_KEYS_KEY,
    ToolsStoreUnavailable,
)

from ._config_flow_core import add_options_entry, add_tools_entry
from ._file_services import make_file_service


@pytest.mark.parametrize(
    ("key", "code"),
    [
        (ALLOWED_PATHS_KEY, "allowed_paths_store_unavailable"),
        (EXTRA_YAML_KEYS_KEY, "extra_yaml_keys_store_unavailable"),
    ],
)
async def test_quarantined_store_prevents_an_empty_backup(
    hass: HomeAssistant, tmp_path: Path, key: str, code: str
) -> None:
    hass.config.config_dir = str(tmp_path)
    add_options_entry(hass, data={"webhook_id": "test", "secret_path": "/test"})
    await add_tools_entry(hass)
    bundle = await settings_transfer.async_export(hass)
    assert bundle["entries"]["tools"] == {"allowed_dirs": [], "extra_yaml_keys": []}
    path = Path(Store(hass, 1, key).path)
    path.parent.mkdir(exist_ok=True)
    path.with_name(f"{key}.corrupt.2026-11-01").write_text("{", encoding="utf-8")
    with pytest.raises(ToolsStoreUnavailable) as err:
        await settings_transfer.async_export(hass)
    assert err.value.code == code

    # A genuine replacement clears the error even if Core kept the old file.
    field = "paths" if key == ALLOWED_PATHS_KEY else "keys"
    await Store(hass, 1, key).async_save({field: []})
    assert await settings_transfer.async_export(hass) == bundle


async def test_ordinary_save_keeps_cache_when_persistence_drops_the_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = await make_file_service(hass)
    original = (await call(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"]

    async def drop_write(store, data):
        assert store.key == EXTRA_YAML_KEYS_KEY

    monkeypatch.setattr(Store, "async_save", drop_write)
    with pytest.raises(ToolsStoreUnavailable) as err:
        await call(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": ["unused_test_section"]})
    assert err.value.code == "extra_yaml_keys_store_unavailable"
    assert (await call(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == original
