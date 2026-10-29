"""The LLM API's executor warm-up loads what the MCP SDK imports lazily.

The SDK (2.x in Home Assistant 2026.11) imports jsonschema on the first tool
call that validates an output schema, and that import scans and reads its
metaschema files: Home Assistant reports it as a blocking call in the event
loop unless the warm-up already did it on the executor.
"""

from __future__ import annotations

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402


def test_warm_up_imports_jsonschema_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []
    monkeypatch.setattr(llm_api.importlib, "import_module", imported.append)
    monkeypatch.setattr(llm_api, "_schema_converter", lambda: None)

    llm_api._import_mcp_sdk()

    assert "jsonschema" in imported
