"""GitHub as the Marketplace reaches it, for the store tool tests.

The Marketplace (``_hacs_marketplace``) reads a repository from GitHub's REST
API through its GitHub library. A test that needs GitHub to answer serves that
API with ``aioclient_mock``, below the library, so every step that reads
GitHub, and every ``marketplace/*`` reply, is still Core's.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from ._hacs_marketplace import add_marketplace

_API = "https://api.github.com/repos"
_RAW = r"https://raw\.githubusercontent\.com"
_JSON = {"Content-Type": "application/json; charset=utf-8"}


def serve_card(
    aioclient_mock: AiohttpClientMocker,
    full_name: str,
    github_id: int,
    *,
    archived: bool = False,
) -> None:
    """GitHub's answers for a dashboard card: no releases, its file at the root."""
    name = full_name.split("/")[1]
    serve_repository(
        aioclient_mock, full_name, github_id, (f"{name}.js",), archived=archived
    )


def serve_repository(
    aioclient_mock: AiohttpClientMocker,
    full_name: str,
    github_id: int,
    files: tuple[str, ...],
    *,
    archived: bool = False,
    releases: tuple[str, ...] = (),
    renamed_from: str | None = None,
) -> None:
    """GitHub's answers for a repository with ``files`` and a ``hacs.json``
    at every ref, and ``releases`` (tags, newest first). ``renamed_from`` is
    a name it had before, under which GitHub answers with the current one.

    Call it before the Marketplace is added: ``aioclient_mock`` answers with
    the first match, and ``add_marketplace`` refuses every other request.
    """
    owner, name = full_name.split("/")
    repo = f"{_API}/{full_name}"
    manifest_json = json.dumps({"name": name})
    manifest = base64.b64encode(manifest_json.encode()).decode()
    directories = sorted(
        {
            "/".join(path.split("/")[:depth])
            for path in files
            for depth in range(1, path.count("/") + 1)
        }
    )
    tree = [
        {"path": path, "type": "tree", "mode": "040000", "sha": "3" * 40}
        for path in directories
    ] + [
        {"path": path, "type": "blob", "mode": "100644", "sha": "1" * 40}
        for path in ("hacs.json", *files)
    ]
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
        f"{repo}/releases": [
            {
                "tag_name": tag,
                "body": f"Release notes for {tag}",
                "name": tag,
                "draft": False,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
                "assets": [],
            }
            for tag in releases
        ],
        f"{repo}/contents/hacs.json": {
            "type": "file",
            "encoding": "base64",
            "name": "hacs.json",
            "path": "hacs.json",
            "content": manifest,
        },
        f"{repo}/branches/main": {"name": "main", "commit": {"sha": "2" * 40}},
    }
    if renamed_from:
        answers[f"{_API}/{renamed_from}"] = answers[repo]
    for url, body in answers.items():
        aioclient_mock.get(url, text=json.dumps(body), headers=_JSON)
    aioclient_mock.get(
        re.compile(rf"{re.escape(repo)}/git/trees/.*"),
        text=json.dumps({"sha": "0" * 40, "truncated": False, "tree": tree}),
        headers=_JSON,
    )
    # An install reads the hacs.json of the version it installs raw.
    aioclient_mock.get(
        re.compile(rf"{_RAW}/{re.escape(full_name)}/.*/hacs\.json"),
        text=manifest_json,
    )


def serve_rate_limit(aioclient_mock: AiohttpClientMocker, full_name: str) -> None:
    """GitHub refuses ``full_name`` with its rate-limit answer. Call it before
    the Marketplace is added."""
    aioclient_mock.get(
        f"{_API}/{full_name}",
        status=403,
        text=json.dumps({"message": "API rate limit exceeded for 127.0.0.1."}),
        headers=_JSON,
    )


def serve_integration(
    aioclient_mock: AiohttpClientMocker,
    full_name: str,
    github_id: int,
    domain: str,
    *,
    manifest_error: str | None = None,
) -> None:
    """GitHub's answers for a repository holding the integration ``domain``,
    its ``manifest.json`` included, and the raw files an install downloads.
    With ``manifest_error`` GitHub answers the read of that ``manifest.json``
    with a server error carrying it. Call it before the Marketplace is added."""
    path = f"custom_components/{domain}/manifest.json"
    manifest = json.dumps({"domain": domain, "name": domain, "version": "1.0.0"})
    answer: dict[str, Any] = {
        "type": "file",
        "encoding": "base64",
        "name": "manifest.json",
        "path": path,
        "content": base64.b64encode(manifest.encode()).decode(),
    }
    aioclient_mock.get(
        f"{_API}/{full_name}/contents/{path}",
        status=502 if manifest_error else 200,
        text=json.dumps({"message": manifest_error} if manifest_error else answer),
        headers=_JSON,
    )
    aioclient_mock.get(
        re.compile(rf"{_RAW}/{re.escape(full_name)}/.*/{re.escape(path)}"),
        text=manifest,
    )
    serve_repository(
        aioclient_mock,
        full_name,
        github_id,
        (path, f"custom_components/{domain}/__init__.py"),
    )
    serve_files(aioclient_mock, full_name)


def serve_files(
    aioclient_mock: AiohttpClientMocker, full_name: str, **answer: Any
) -> None:
    """GitHub's raw content of each of ``full_name``'s files: ``answer`` is
    ``aioclient_mock``'s (``side_effect=`` to hold it), a few bytes without
    it. Call it before the Marketplace is added."""
    aioclient_mock.get(
        re.compile(rf"{_RAW}/{re.escape(full_name)}/.*"),
        **(answer or {"text": f"// {full_name}"}),
    )


USER_CODE = "ABCD-1234"


def serve_github_sign_in(
    aioclient_mock: AiohttpClientMocker, *token_answers: dict[str, Any]
) -> None:
    """GitHub's device sign-in: a code for the user, then ``token_answers`` in
    turn each time the code is checked, the last one from then on
    (``{"error": "authorization_pending"}`` while the user has not entered
    it). Call it before the Marketplace is added."""
    device = {
        "device_code": "device-code",
        "user_code": USER_CODE,
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 5,
    }
    answers = list(token_answers)

    async def check_code(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        body = answers.pop(0) if len(answers) > 1 else answers[0]
        return AiohttpClientMockResponse(
            method, url, text=json.dumps(body), headers=_JSON
        )

    aioclient_mock.post(
        re.compile(r"https://github\.com/login/device/code.*"),
        text=json.dumps(device),
        headers=_JSON,
    )
    aioclient_mock.post(
        re.compile(r"https://github\.com/login/oauth/access_token.*"),
        side_effect=check_code,
    )


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
