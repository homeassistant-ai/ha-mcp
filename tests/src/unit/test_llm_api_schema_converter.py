"""Which codec converts the server's tool schemas for Home Assistant (#1745).

Stable Core converts with ``voluptuous_openapi``; Core 2026.9+ ships
``probatio`` instead. The component prefers the first and falls back to the
second, once per process and never on the event loop.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import probatio
import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import llm_api

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


@pytest.fixture(autouse=True)
def _resolve_the_converter_afresh() -> Iterator[None]:
    """The choice is memoised for the process; each test starts unresolved."""
    llm_api.reset_schema_converter()
    yield
    llm_api.reset_schema_converter()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == llm_api.__name__ and record.levelno == logging.WARNING
    ]


def test_the_voluptuous_openapi_converter_is_used_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=llm_api.__name__):
        converted = llm_api.convert_to_voluptuous(_SCHEMA)

    assert isinstance(converted, vol.Schema)
    assert converted({"query": "kitchen"}) == {"query": "kitchen"}
    with pytest.raises(vol.Invalid):
        converted({})
    assert _warnings(caplog) == []


def test_a_core_without_voluptuous_openapi_converts_with_probatio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", None)

    converted = llm_api.convert_to_voluptuous(_SCHEMA)

    assert isinstance(converted, probatio.Schema)
    assert converted({"query": "kitchen"}) == {"query": "kitchen"}
    with pytest.raises(probatio.Invalid):
        converted({})


def test_the_fallback_announces_which_codec_is_converting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing else reports that the component runs on the other codec.

    A requirement that failed to install never gets here: Home Assistant
    logs that itself and abandons the integration before importing it. What
    does get here -- skip_pip, or a deps tree that lost the package --
    produces no message anywhere else, and the conversion then succeeds
    quietly with a codec that retypes integers.
    """
    monkeypatch.setitem(sys.modules, "voluptuous_openapi", None)

    with caplog.at_level(logging.DEBUG, logger=llm_api.__name__):
        llm_api.convert_to_voluptuous(_SCHEMA)

    [warning] = _warnings(caplog)
    assert "voluptuous-openapi" in warning


def test_a_failure_inside_the_converter_is_not_mistaken_for_its_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back would hide a broken install behind a working codec."""
    package = tmp_path / "voluptuous_openapi"
    package.mkdir()
    (package / "__init__.py").write_text("import legacy_dependency_not_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in sys.modules if n.partition(".")[0] == "voluptuous_openapi"]:
        monkeypatch.delitem(sys.modules, name)

    with pytest.raises(ModuleNotFoundError, match="legacy_dependency_not_installed"):
        llm_api.convert_to_voluptuous(_SCHEMA)


async def test_the_converter_is_imported_once_and_off_the_event_loop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing it on the loop would trip Home Assistant's blocking-call check."""
    loop_thread = threading.get_ident()
    watched = {"mcp.client.session", "mcp.client.streamable_http", "voluptuous_openapi"}
    imports: list[tuple[str, int]] = []
    real_import_module = importlib.import_module

    def recording_import_module(name: str, package: str | None = None) -> Any:
        if name in watched:
            imports.append((name, threading.get_ident()))
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", recording_import_module)

    assert await llm_api.async_probe_mcp_sdk(hass)
    llm_api.convert_to_voluptuous(_SCHEMA)
    llm_api.convert_to_voluptuous(_SCHEMA)

    assert [name for name, _ in imports] == [
        "mcp.client.session",
        "mcp.client.streamable_http",
        "voluptuous_openapi",
    ]
    assert all(thread != loop_thread for _, thread in imports)
