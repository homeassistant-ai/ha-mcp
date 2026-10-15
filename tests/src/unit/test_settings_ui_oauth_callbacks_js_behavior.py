"""Settings-page editor for the embedded server's OAuth callback allowlist (#2427).

The page's other behaviour lives in ``test_settings_ui_js_behavior.py``; this
surface has its own module because that one is at its size ratchet.
"""

from __future__ import annotations

import json
import re

from . import test_settings_ui_js_behavior as js
from ._js_harness import HarnessResult, run_script
from .test_settings_ui_js_behavior import DEFAULT_FETCHES, MIN_DOM

# The rendered settings script, shared with the page's behaviour suite.
settings_script = js.settings_script

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
                "document.getElementById('oauthCallbacksSave').click();"
                "await new Promise(r => setTimeout(r, 0));"
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

    def test_restore_default_asks_the_server_to_reset(self, settings_script):
        result = _oauth_run(
            settings_script,
            {**_OAUTH_STATE, "customized": True},
            post={"status": 200, "json": {**_OAUTH_STATE, "saved": True}},
            invoke=(
                "document.getElementById('oauthCallbacksReset').click();"
                "await new Promise(r => setTimeout(r, 0));"
            ),
        )
        posts = [
            json.loads(f["body"])
            for f in result.fetches_to("/api/settings/oauth-callbacks")
            if f["method"] == "POST"
        ]
        assert posts == [{"reset": True}]

    def test_a_list_that_cannot_load_says_so_instead_of_vanishing(
        self, settings_script
    ):
        # An expired panel session or a proxy error must not hide the editor
        # without a word.
        result = run_script(
            settings_script,
            initial_html=_OAUTH_DOM,
            fetch_map={
                **DEFAULT_FETCHES,
                "/api/settings/oauth-callbacks": {
                    "status": 401,
                    "body": "Unauthorized",
                },
            },
            invoke="await loadOAuthCallbacks();",
        )
        assert not _section_hidden(result.dom)
        assert "HTTP 401" in result.dom

    def test_a_proxy_error_page_reports_its_status(self, settings_script):
        result = _oauth_run(
            settings_script,
            _OAUTH_STATE,
            post={"status": 502, "body": "<html>Bad gateway</html>"},
            invoke=(
                "document.getElementById('oauthCallbacksSave').click();"
                "await new Promise(r => setTimeout(r, 0));"
            ),
        )
        status = re.search(
            r'<div id="oauthCallbacksStatus"[^>]*>(.*?)</div>', result.dom, re.S
        )
        assert status and "HTTP 502" in status.group(1)
        assert "SyntaxError" not in status.group(1)
