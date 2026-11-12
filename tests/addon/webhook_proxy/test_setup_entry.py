"""Setting the proxy's config entry up, failing it loudly, and unloading it."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import (
    Upstream,
    proxy_config_path,
    record_client_sessions,
    set_up_proxy,
    start_proxy,
)
from ._relay import WEBHOOK_ID, WEBHOOK_PATH, existing_webhook_handler, relay_config
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")

REAL_TOKEN = "zctpwlX7ZkIAr7oqdfLPxw"  # the 22-character shape ha-mcp generates


@pytest.mark.parametrize(
    "target_url",
    [
        pytest.param(f"http://172.30.33.1:9583/private_{REAL_TOKEN}", id="real-token"),
        pytest.param(f"https://example.com/private_{REAL_TOKEN}", id="https"),
        pytest.param("http://h:9583/private_" + "a" * 16, id="16-char-floor"),
        pytest.param("http://localhost:8123/api/", id="custom-server-path"),
    ],
)
async def test_well_formed_target_url_lets_the_proxy_load(
    hass: HomeAssistant, variant: Variant, fs_root: Path, target_url: str
) -> None:
    """A user whose ha-mcp URL is well formed, or who points the proxy at a
    custom MCP server path, gets a loaded integration."""
    entry = await start_proxy(
        hass, variant, fs_root, {"target_url": target_url, "webhook_id": WEBHOOK_ID}
    )

    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("target_url", "reason"),
    [
        pytest.param("http://h/private_" + "a" * 15, "secret path", id="15-char-token"),
        pytest.param(
            "http://h/private_has%20space_aaaaaaa", "secret path", id="bad-token-chars"
        ),
        pytest.param("ftp://h/private_" + "a" * 16, "scheme", id="ftp-scheme"),
        pytest.param("http:///private_" + "a" * 16, "host", id="no-host"),
        pytest.param("http://h/private_" + "a" * 16 + "?foo=bar", "query", id="query"),
        pytest.param("http://h/private_" + "a" * 16 + "#frag", "fragment", id="frag"),
        pytest.param(
            "http://h/private_" + "a" * 16 + ";param", "path parameters", id="params"
        ),
    ],
)
async def test_malformed_target_url_fails_setup_with_the_reason(
    hass: HomeAssistant, variant: Variant, fs_root: Path, target_url: str, reason: str
) -> None:
    """A corrupted ha-mcp URL must not load an integration whose every request
    404s upstream; the entry shows why instead."""
    entry = await start_proxy(
        hass, variant, fs_root, {"target_url": target_url, "webhook_id": WEBHOOK_ID}
    )

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "Invalid target_url" in entry.reason
    assert reason in entry.reason


async def test_truncated_secret_path_registers_no_webhook(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated secret (the failure seen in the wild) leaves the webhook id
    free, nothing relaying to it, and no session open."""
    config = relay_config(upstream, target_url=upstream.target_url("private_ZZZZZZZ"))
    sessions = record_client_sessions(monkeypatch)
    entry = await start_proxy(hass, variant, fs_root, config)
    # Core refuses a second handler for an id that is still registered.
    webhook.async_register(hass, "other", "Other", WEBHOOK_ID, existing_webhook_handler)
    client = await hass_client_no_auth()

    response = await client.post(WEBHOOK_PATH, data=b"{}")

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert (await response.json())["handled_by"] == "existing"
    assert upstream.requests == []
    assert all(session.closed for session in sessions)


async def test_rejected_target_url_never_logs_the_secret(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The secret path is the credential to the user's ha-mcp server; a
    validation failure logs it masked."""
    secret_tail = "ZZZZZZZ_real_secret_value"
    target_url = f"http://h:9583/private_{secret_tail}_but_with_bad_chars!"

    await start_proxy(
        hass, variant, fs_root, {"target_url": target_url, "webhook_id": WEBHOOK_ID}
    )

    assert "validation failed" in caplog.text
    assert "/private_********" in caplog.text
    assert secret_tail not in caplog.text


@pytest.mark.parametrize(
    "blank", [{"target_url": ""}, {"webhook_id": ""}], ids=["target_url", "webhook_id"]
)
async def test_config_missing_a_required_value_fails_setup(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    blank: dict[str, str],
) -> None:
    """A config without its target or webhook id fails loudly instead of
    loading an integration that serves nothing."""
    entry = await start_proxy(
        hass, variant, fs_root, {**relay_config(upstream), **blank}
    )

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "Missing target_url or webhook_id" in entry.reason


async def test_webhook_id_already_taken_fails_setup_and_closes_session(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Core refuses the webhook (its id is already registered), setup
    fails with the reason and does not leak the relay session."""
    assert await async_setup_component(hass, webhook.DOMAIN, {})
    webhook.async_register(hass, "other", "Other", WEBHOOK_ID, existing_webhook_handler)

    sessions = record_client_sessions(monkeypatch)
    entry = await start_proxy(hass, variant, fs_root, relay_config(upstream))

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "Failed to register webhook endpoint" in entry.reason
    assert sessions
    assert all(session.closed for session in sessions)


@pytest.mark.parametrize("damage", ["corrupted", "unreadable"])
async def test_damaged_config_file_fails_setup_instead_of_looking_fresh(
    hass: HomeAssistant, variant: Variant, fs_root: Path, damage: str
) -> None:
    """A config file that exists but cannot be read is a broken install, not
    a fresh one: setup fails and says which file to regenerate."""
    path = proxy_config_path(fs_root, variant)
    if damage == "corrupted":
        path.write_text("{ ")
    else:
        path.mkdir()

    entry = await set_up_proxy(hass, variant)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "Failed to read" in entry.reason


async def test_fresh_install_without_config_loads_and_relays_nothing(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    upstream: Upstream,
) -> None:
    """Before the app has written its config the entry loads (it is waiting
    for the app), and no webhook relays anywhere."""
    entry = await set_up_proxy(hass, variant)
    client = await hass_client_no_auth()

    await client.post(WEBHOOK_PATH, data=b"{}")

    assert entry.state is ConfigEntryState.LOADED
    assert upstream.requests == []


async def test_proxy_without_oauth_relays_unauthenticated_requests(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """With OAuth off the webhook URL is the only credential: a request
    without a bearer reaches ha-mcp and its answer comes back."""
    await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()

    response = await client.post(
        WEBHOOK_PATH,
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status == 200
    assert await response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert [r.body for r in upstream.requests] == [
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    ]


async def test_unloading_a_waiting_entry_succeeds(
    hass: HomeAssistant, variant: Variant
) -> None:
    """An entry that loaded with no config yet unloads cleanly (the app
    removes and re-adds it on every start)."""
    entry = await set_up_proxy(hass, variant)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_stops_relaying_and_closes_the_session(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the app stops, its webhook and read-only alias forward nothing
    and the relay session is released."""
    sessions = record_client_sessions(monkeypatch)
    entry = await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await client.post(WEBHOOK_PATH, data=b"{}")
    readonly = await client.post(f"{WEBHOOK_PATH}/readonly", data=b"{}")

    assert readonly.status == 404
    assert upstream.requests == []
    assert sessions
    assert all(session.closed for session in sessions)


async def test_relay_session_has_no_wall_clock_bound(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP response stream may stay open indefinitely (the spec's
    ``subscriptions/listen``); a total time bound would cut a healthy stream
    and force the client to re-subscribe."""
    sessions = record_client_sessions(monkeypatch)
    await start_proxy(hass, variant, fs_root, relay_config(upstream))

    (relay,) = sessions
    assert relay.timeout.total is None


async def test_relay_session_still_bounds_dead_streams_and_connects(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a total bound, an idle-read bound is what ends a dead stream,
    and a pool-acquisition bound is what stops a pool exhausted by long-lived
    streams from hanging every new request."""
    sessions = record_client_sessions(monkeypatch)
    await start_proxy(hass, variant, fs_root, relay_config(upstream))

    (relay,) = sessions
    assert relay.timeout.sock_read is not None
    assert relay.timeout.sock_connect is not None
    assert relay.timeout.connect is not None
