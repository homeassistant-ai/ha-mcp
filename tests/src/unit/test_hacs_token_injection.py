"""The pre-boot edits of Core's stored config entries for the E2E images.

``inject_hacs_token`` is the shared transform behind both CI injection paths
(the testcontainer seed copy in ``e2e/conftest.py`` and the HAOS qcow2
pre-boot edit in ``haos_runtime.inject_hacs_token_in_qcow2``). It
authenticates HACS in CI so repository adds don't ride the shared-IP 60 req/h
unauthenticated GitHub budget, the long-standing HACS-install e2e flake
("GitHub Ratelimit error" in the HA core log).

Every document below is what Core writes to ``.storage/core.config_entries``:
the entries are Core's own storage records of real config entries.

``haos_runtime`` is loaded by file path: its module-level imports are
stdlib-only, so this collects everywhere, without assuming ``tests/src``
is importable as a package.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.config_entries import (
    STORAGE_KEY,
    STORAGE_VERSION,
    STORAGE_VERSION_MINOR,
)
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant.common import MockConfigEntry

HAOS_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "haos_runtime.py"


def _load_haos_runtime() -> ModuleType:
    spec = importlib.util.spec_from_file_location("haos_runtime", HAOS_RUNTIME_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


haos_runtime = _load_haos_runtime()


def _stored(entry: MockConfigEntry) -> dict[str, Any]:
    """The entry as Core stores it."""
    stored: dict[str, Any] = json.loads(json_bytes(entry.as_storage_fragment))
    return stored


def _doc(*entries: MockConfigEntry) -> dict[str, Any]:
    """A ``.storage/core.config_entries`` document holding ``entries``."""
    return {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_VERSION_MINOR,
        "key": STORAGE_KEY,
        "data": {"entries": [_stored(entry) for entry in entries]},
    }


def _server_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain="ha_mcp_tools",
        entry_id=haos_runtime.HA_MCP_SERVER_ENTRY_ID,
        data={"entry_type": "server"},
        options={},
    )


def test_hacs_entry_gets_the_token_and_keeps_the_rest_of_the_entry() -> None:
    doc = _doc(
        MockConfigEntry(domain="sun"),
        MockConfigEntry(domain="hacs", options={"experimental": False}),
    )
    before = copy.deepcopy(doc["data"]["entries"])

    assert haos_runtime.inject_hacs_token(doc, "ghs_test_token") is True

    hacs = doc["data"]["entries"][1]
    # Same shape HACS' own config flow persists after device-flow auth.
    assert hacs["data"] == {"token": "ghs_test_token"}
    # Only ``data`` is replaced, the rest of the entry is untouched.
    assert {k: v for k, v in hacs.items() if k != "data"} == {
        k: v for k, v in before[1].items() if k != "data"
    }
    assert doc["data"]["entries"][0] == before[0]


def test_a_stale_token_is_replaced() -> None:
    doc = _doc(MockConfigEntry(domain="hacs", data={"token": "stale"}))
    assert haos_runtime.inject_hacs_token(doc, "fresh") is True
    assert doc["data"]["entries"][0]["data"] == {"token": "fresh"}


def test_a_document_without_hacs_is_left_untouched() -> None:
    doc = _doc(MockConfigEntry(domain="sun"), MockConfigEntry(domain="demo"))
    before = copy.deepcopy(doc)
    assert haos_runtime.inject_hacs_token(doc, "tok") is False
    assert doc == before


def test_qcow2_injector_is_noop_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No GITHUB_TOKEN -> no guestfish invocation, no exception."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("guestfish must not run without GITHUB_TOKEN")

    monkeypatch.setattr(haos_runtime.subprocess, "run", _fail)
    haos_runtime.inject_hacs_token_in_qcow2(tmp_path / "image.qcow2")


# set_embedded_server_pip_spec (#1527): matched by entry_id, because the
# server entry shares its domain with the tools entry.


def test_pip_spec_and_admin_token_reach_only_the_server_entry() -> None:
    doc = _doc(
        MockConfigEntry(domain="ha_mcp_tools", entry_id="other"), _server_entry()
    )

    assert (
        haos_runtime.set_embedded_server_pip_spec(doc, "file:///w.whl", "tok") is True
    )

    tools, server = doc["data"]["entries"]
    assert server["options"]["pip_spec"] == "file:///w.whl"
    # #2427: the component no longer creates its own administrator.
    assert server["data"]["admin_token"] == "tok"
    assert server["data"]["entry_type"] == "server"
    assert "pip_spec" not in tools["options"]
    assert "admin_token" not in tools["data"]


def test_pip_spec_without_a_server_entry_leaves_the_document_untouched() -> None:
    doc = _doc(
        MockConfigEntry(
            domain="ha_mcp_tools", entry_id="other", options={"pip_spec": "keep"}
        )
    )
    before = copy.deepcopy(doc)
    assert haos_runtime.set_embedded_server_pip_spec(doc, "x", "tok") is False
    assert doc == before


# remove_tools_entry (#2292): the transform behind the no-tools lanes' qcow2
# pre-boot edit (remove_tools_entry_in_qcow2), discriminated the way the
# component itself dispatches (data.entry_type == "server" is the server
# entry; anything else under the ha_mcp_tools domain is the tools entry).


def test_the_tools_entry_is_removed_and_the_server_entry_kept() -> None:
    doc = _doc(
        MockConfigEntry(domain="sun"),
        MockConfigEntry(domain="ha_mcp_tools", entry_id="e2e_test_ha_mcp_tools_entry"),
        _server_entry(),
    )

    assert haos_runtime.remove_tools_entry(doc) == ["e2e_test_ha_mcp_tools_entry"]

    remaining = doc["data"]["entries"]
    assert [e["domain"] for e in remaining] == ["sun", "ha_mcp_tools"]
    assert remaining[1]["entry_id"] == haos_runtime.HA_MCP_SERVER_ENTRY_ID


def test_a_document_without_a_tools_entry_is_left_untouched() -> None:
    doc = _doc(MockConfigEntry(domain="sun"), _server_entry())
    before = copy.deepcopy(doc)
    assert haos_runtime.remove_tools_entry(doc) == []
    assert doc == before
