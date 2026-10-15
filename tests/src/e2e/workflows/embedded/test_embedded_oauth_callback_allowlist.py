"""A callback-list edit reaches the next none-mode sign-in in real HA (#2427).

The unit tests cover each piece against fakes: the WebSocket command, the
options write, the reload filter and the provider reading ``entry.options``.
This proves the wiring in Home Assistant itself: a save through
``ha_mcp_tools/oauth_callbacks_update`` changes what the live ``/authorize``
answers, with no restart in between.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.embedded_only

_CALLBACK = "https://chatgpt.example/connector/cb"
_UPDATE = "ha_mcp_tools/oauth_callbacks_update"


def _authorize(base_url: str, callback: str) -> requests.Response:
    return requests.get(
        f"{base_url}/api/ha_mcp_tools/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "e2e",
            "redirect_uri": callback,
            "state": "s",
            "code_challenge": "A" * 43,
            "code_challenge_method": "S256",
        },
        allow_redirects=False,
        timeout=30,
    )


async def test_a_saved_callback_is_honoured_by_the_next_sign_in(
    ha_client, ha_container_with_fresh_config
):
    base_url = ha_container_with_fresh_config["base_url"]
    refused = _authorize(base_url, _CALLBACK)
    assert refused.status_code == 400, refused.text[:200]
    assert "Location" not in refused.headers

    try:
        saved = await ha_client.send_websocket_message(
            {"type": _UPDATE, "allowlist": [_CALLBACK]}
        )
        assert saved.get("success") and saved["result"]["saved"], saved

        allowed = _authorize(base_url, _CALLBACK)
        assert allowed.status_code == 302, allowed.text[:200]
        assert allowed.headers["Location"].startswith(f"{_CALLBACK}?code=")
    finally:
        reset = await ha_client.send_websocket_message({"type": _UPDATE, "reset": True})
        assert reset.get("success"), reset

    assert _authorize(base_url, _CALLBACK).status_code == 400
