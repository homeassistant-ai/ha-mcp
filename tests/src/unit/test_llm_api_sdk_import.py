"""The LLM API warms the SDK's lazy jsonschema import off Core's event loop."""

from __future__ import annotations

import threading

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import llm_api


async def test_warm_up_imports_jsonschema_off_the_loop(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[tuple[str, int]] = []
    import_module = llm_api.importlib.import_module
    loop_thread = threading.get_ident()

    def record_import(name: str, package: str | None = None):
        imported.append((name, threading.get_ident()))
        return import_module(name, package)

    monkeypatch.setattr(llm_api.importlib, "import_module", record_import)

    assert await llm_api.async_probe_mcp_sdk(hass)
    jsonschema_threads = [thread for name, thread in imported if name == "jsonschema"]
    assert jsonschema_threads
    assert all(thread != loop_thread for thread in jsonschema_threads)
