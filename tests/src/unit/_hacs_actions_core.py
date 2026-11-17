"""GitHub as the Marketplace reaches it, for the HACS action tests.

The Marketplace (``_hacs_marketplace``) reads a repository from GitHub's REST
API through its GitHub library. A test that needs GitHub to answer serves that
API with ``aioclient_mock``, below the library, so every step that reads
GitHub, and every ``marketplace/*`` reply, is still Core's.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ._hacs_marketplace import add_marketplace

_API = "https://api.github.com/repos"
_JSON = {"Content-Type": "application/json; charset=utf-8"}


def serve_card(
    aioclient_mock: AiohttpClientMocker,
    full_name: str,
    github_id: int,
    *,
    archived: bool = False,
) -> None:
    """GitHub's answers for a dashboard card: no releases, its file at the root.

    Call it before the Marketplace is added: ``aioclient_mock`` answers with
    the first match, and ``add_marketplace`` refuses every other request.
    """
    owner, name = full_name.split("/")
    repo = f"{_API}/{full_name}"
    manifest = base64.b64encode(json.dumps({"name": name}).encode()).decode()
    answers: dict[str, Any] = {
        repo: {
            "id": github_id,
            "name": name,
            "full_name": full_name,
            "default_branch": "main",
            "archived": archived,
            "description": f"The {name} card",
            "topics": [],
            "stargazers_count": 1,
            "pushed_at": "2026-01-01T00:00:00Z",
            "owner": {"login": owner},
        },
        f"{repo}/releases": [],
        f"{repo}/git/trees/main": {
            "sha": "0" * 40,
            "truncated": False,
            "tree": [
                {"path": path, "type": "blob", "mode": "100644", "sha": "1" * 40}
                for path in ("hacs.json", f"{name}.js")
            ],
        },
        f"{repo}/contents/hacs.json": {
            "type": "file",
            "encoding": "base64",
            "name": "hacs.json",
            "path": "hacs.json",
            "content": manifest,
        },
        f"{repo}/branches/main": {"name": "main", "commit": {"sha": "2" * 40}},
    }
    for url, body in answers.items():
        aioclient_mock.get(url, text=json.dumps(body), headers=_JSON)


def add_connected_marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    repositories: dict[str, dict[str, Any]],
) -> MockConfigEntry:
    """The Marketplace entry with a GitHub account connected, not set up yet."""
    entry = add_marketplace(hass, aioclient_mock, hass_storage, repositories)
    hass.config_entries.async_update_entry(entry, data={CONF_TOKEN: "github-token"})
    return entry
