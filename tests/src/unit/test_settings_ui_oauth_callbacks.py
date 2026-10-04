"""Settings-panel editor for the embedded server's OAuth callback allowlist (#2427)."""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.settings_ui import _handlers_oauth_callbacks as oc
from ha_mcp.settings_ui import build_settings_handlers

from . import test_settings_ui_js_behavior as js
from ._js_harness import HarnessResult, run_script
from .test_settings_ui_js_behavior import DEFAULT_FETCHES, MIN_DOM

# The rendered settings script, shared with the page's behaviour suite.
settings_script = js.settings_script

CALLBACK = "https://chatgpt.example/cb"
STATE = {
    "entry_id": "srv1",
    "allowlist": [CALLBACK],
    "default_allowlist": ["https://claude.ai/api/mcp/auth_callback"],
    "customized": True,
    "applies": True,
}


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")


@pytest.fixture
def component(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record the component commands the handlers send; answer like the component."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def command(server: Any, name: str, **fields: Any) -> dict[str, Any]:
        calls.append((name, fields))
        if fields.get("allowlist") == ["http://not-loopback/cb"]:
            return {**STATE, "saved": False, "invalid": fields["allowlist"]}
        return {**STATE, "saved": True, "invalid": []}

    monkeypatch.setattr(oc, "_component_command", command)
    return calls


def _request(body: Any) -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return request


async def _call(name: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    handlers = build_settings_handlers(server=MagicMock())
    response = await handlers[name](_request(body))
    return response.status_code, json.loads(response.body)


async def test_other_install_types_do_not_offer_the_editor(component) -> None:
    status, body = await _call("get_oauth_callbacks")
    assert (status, body["available"]) == (200, False)
    assert component == []


async def test_the_editor_shows_the_list_in_force(embedded, component) -> None:
    _, body = await _call("get_oauth_callbacks")
    assert body["available"] is True
    assert body["allowlist"] == [CALLBACK]


async def test_a_component_without_the_commands_explains_why(
    embedded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def too_old(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise oc._Unavailable("Update the HA-MCP integration in HACS.")

    monkeypatch.setattr(oc, "_component_command", too_old)
    _, body = await _call("get_oauth_callbacks")
    assert body["available"] is False
    assert "Update" in body["reason"]


async def test_a_save_sends_the_list_to_the_component(embedded, component) -> None:
    status, body = await _call("save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 200
    assert component == [(oc.WS_OAUTH_CALLBACKS_UPDATE, {"allowlist": [CALLBACK]})]


async def test_restoring_the_default_asks_the_component_to_reset(
    embedded, component
) -> None:
    await _call("save_oauth_callbacks", {"reset": True})
    assert component == [(oc.WS_OAUTH_CALLBACKS_UPDATE, {"reset": True})]


async def test_unusable_callbacks_are_named_back(embedded, component) -> None:
    status, body = await _call(
        "save_oauth_callbacks", {"allowlist": ["http://not-loopback/cb"]}
    )
    assert status == 400
    assert body["invalid"] == ["http://not-loopback/cb"]


@pytest.mark.parametrize(
    "body", [{"allowlist": "x"}, {"allowlist": [1]}, {"allowlist": ["x"] * 51}, []]
)
async def test_a_malformed_save_is_refused_before_the_component(
    embedded, component, body: Any
) -> None:
    status, _ = await _call("save_oauth_callbacks", body)
    assert status == 400
    assert component == []


async def test_a_save_outside_the_embedded_server_is_refused(component) -> None:
    status, _ = await _call("save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 409
    assert component == []


# ---------------------------------------------------------------------------
# OAuth callback allowlist editor (embedded server, #2427)
# ---------------------------------------------------------------------------

_OAUTH_DOM = MIN_DOM.replace(
    "</body>",
    '<div id="oauthCallbacksSection" hidden>'
    '<div id="oauthCallbacksBody"></div></div></body>',
)
_OAUTH_STATE = {
    "success": True,
    "available": True,
    "allowlist": ["https://claude.ai/api/mcp/auth_callback"],
    "default_allowlist": ["https://claude.ai/api/mcp/auth_callback"],
    "customized": False,
    "applies": True,
}


def _oauth_run(
    settings_script: str, get_json: dict, invoke: str = "", post: dict | None = None
) -> HarnessResult:
    route: dict = {"status": 200, "json": get_json}
    if post is not None:
        route = {"byMethod": {"GET": route, "POST": post}}
    return run_script(
        settings_script,
        initial_html=_OAUTH_DOM,
        fetch_map={**DEFAULT_FETCHES, "/api/settings/oauth-callbacks": route},
        invoke="await loadOAuthCallbacks();" + invoke,
    )


def _section_hidden(dom: str) -> bool:
    tag = re.search(r'<div id="oauthCallbacksSection"[^>]*>', dom)
    assert tag, "section missing from the DOM"
    return "hidden" in tag.group(0)


class TestOAuthCallbackEditor:
    def test_installs_without_the_list_never_see_the_section(self, settings_script):
        result = _oauth_run(settings_script, {"success": True, "available": False})
        assert _section_hidden(result.dom)

    def test_the_embedded_server_shows_the_list_in_force(self, settings_script):
        result = _oauth_run(
            settings_script,
            _OAUTH_STATE,
            invoke=(
                "document.body.dataset.value = "
                "document.getElementById('oauthCallbacksInput').value;"
            ),
        )
        assert not _section_hidden(result.dom)
        assert 'data-value="https://claude.ai/api/mcp/auth_callback"' in result.dom

    def test_save_sends_one_callback_per_line(self, settings_script):
        result = _oauth_run(
            settings_script,
            _OAUTH_STATE,
            post={"status": 200, "json": {**_OAUTH_STATE, "saved": True}},
            invoke=(
                "document.getElementById('oauthCallbacksInput').value = "
                "' https://a.example/cb \\n\\nhttp://127.0.0.1/cb';"
                "await saveOAuthCallbacks({allowlist: document.getElementById("
                "'oauthCallbacksInput').value.split('\\n').map(s => s.trim())"
                ".filter(Boolean)});"
            ),
        )
        posts = [
            f
            for f in result.fetches_to("/api/settings/oauth-callbacks")
            if f["method"] == "POST"
        ]
        assert [json.loads(p["body"]) for p in posts] == [
            {"allowlist": ["https://a.example/cb", "http://127.0.0.1/cb"]}
        ]

    def test_a_refused_save_names_the_unusable_callback(self, settings_script):
        result = _oauth_run(
            settings_script,
            _OAUTH_STATE,
            post={
                "status": 400,
                "json": {"success": False, "invalid": ["http://lan.example/cb"]},
            },
            invoke=(
                "document.getElementById('oauthCallbacksSave').click();"
                "await new Promise(r => setTimeout(r, 0));"
            ),
        )
        status = re.search(
            r'<div id="oauthCallbacksStatus"[^>]*>(.*?)</div>', result.dom, re.S
        )
        assert status and "http://lan.example/cb" in status.group(1)
