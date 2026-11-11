"""Shared helpers for the ha_auth and none-mode (auto-approve) tests.

Both modes are driven the way a client meets them: Core sets the proxy's
config entry up from the file ``start.py`` writes, a reload switches modes,
and every document, redirect and challenge is fetched over Home Assistant's
real HTTP server.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import secrets
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from ._proxy import set_up_proxy, state_path, write_proxy_config
from ._variants import Variant

WEBHOOK_ID = "mcp_auth_modes_webhook"
# Never contacted: tests that relay take the `upstream` fixture's URL instead.
UNUSED_TARGET = "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw"
HA_AUTH: dict[str, Any] = {"mode": "ha_auth"}
LEGACY: dict[str, Any] = {
    "mode": "legacy",
    "client_id": "client-1234567890ABCDEF",
    "client_secret": "secret-much-secret",
}
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
# The click-to-restart Repair both OAuth modes and none mode share.
RESTART_ISSUE_ID = "oauth_restart_required"


def proxy_config(
    *,
    target_url: str = UNUSED_TARGET,
    webhook_id: str = WEBHOOK_ID,
    oauth: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The proxy config file; no ``oauth`` section means OAuth off."""
    config: dict[str, Any] = {"target_url": target_url, "webhook_id": webhook_id}
    if oauth is not None:
        config["oauth"] = dict(oauth)
    config.update(extra)
    return config


async def start_proxy(
    hass: HomeAssistant, fs_root: Path, variant: Variant, config: dict[str, Any]
) -> MockConfigEntry:
    write_proxy_config(fs_root, variant, config)
    return await set_up_proxy(hass, variant)


async def reload_proxy(
    hass: HomeAssistant,
    fs_root: Path,
    variant: Variant,
    entry: MockConfigEntry,
    config: dict[str, Any],
) -> bool:
    """Rewrite the config file and reload the entry, as ``start.py`` does."""
    write_proxy_config(fs_root, variant, config)
    reloaded = await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return reloaded


def submodule(component: ModuleType, name: str) -> ModuleType:
    """The component's own submodule (already loaded by the fixture)."""
    return importlib.import_module(f"{component.__name__}.{name}")


def route_counts(hass: HomeAssistant) -> Counter[str]:
    """How many times each path is bound on Home Assistant's HTTP router.

    Home Assistant cannot drop a bound view until it restarts, and aiohttp lets
    the first route win silently, so a second binding of a path is a shadowed
    duplicate rather than an error.
    """
    return Counter(resource.canonical for resource in hass.http.app.router.resources())


def as_document_urls(variant: Variant) -> list[str]:
    """Every URL the authorization-server document is served at (#1714).

    The four well-known spellings are the ones MCP clients probe for the
    issuer ``<base><oauth_base>``.
    """
    base = variant.oauth_base
    return [
        f"{base}/authorization-server",
        f"/.well-known/oauth-authorization-server{base}",
        f"/.well-known/openid-configuration{base}",
        f"{base}/.well-known/openid-configuration",
        f"{base}/.well-known/oauth-authorization-server",
    ]


def prm_url(webhook_id: str = WEBHOOK_ID) -> str:
    """RFC 9728 §3.1 location of the webhook's protected-resource document."""
    return f"/.well-known/oauth-protected-resource/api/webhook/{webhook_id}"


def scoped_urls(variant: Variant) -> list[str]:
    base = variant.oauth_base
    return [f"{base}/authorize", f"{base}/token", f"{base}/revoke", f"{base}/register"]


async def fetch_json(
    client: TestClient, path: str, host: str = "proxy.example"
) -> tuple[int, Any]:
    response = await client.get(path, headers={"Host": host})
    body = await response.json(content_type=None) if response.status == 200 else None
    return response.status, body


def restart_issue(hass: HomeAssistant, variant: Variant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(variant.domain, RESTART_ISSUE_ID)


def marker_path(fs_root: Path, variant: Variant) -> Path:
    return state_path(fs_root, variant.oauth_marker)


def break_dcr_secret(fs_root: Path, variant: Variant) -> None:
    """Make the DCR signing key unreadable: a directory where the file goes."""
    state_path(fs_root, f"/config/.{variant.component}_dcr_secret").mkdir()


def pkce_pair() -> tuple[str, str]:
    """A valid (code_verifier, code_challenge) pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _b64url_json(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def token_core_cannot_validate() -> str:
    """An unsigned JWT whose ``iss`` is a list.

    Core's ``async_validate_access_token`` decodes it without verifying, then
    looks the ``iss`` up as a refresh-token id and raises ``TypeError``
    (unhashable list) instead of answering ``None``.
    """
    header = _b64url_json({"alg": "HS256", "typ": "JWT"})
    claims = _b64url_json({"iss": ["not-a-refresh-token-id"]})
    return f"{header}.{claims}.c2ln"


def record_sessions(monkeypatch: pytest.MonkeyPatch) -> list[aiohttp.ClientSession]:
    """Record every aiohttp session created from now on (to prove none leak)."""
    created: list[aiohttp.ClientSession] = []
    session_class: Callable[..., aiohttp.ClientSession] = aiohttp.ClientSession

    def recording_session(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = session_class(*args, **kwargs)
        created.append(session)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", recording_session)
    return created
