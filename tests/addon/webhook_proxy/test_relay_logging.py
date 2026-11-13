"""The "Log inbound requests" toggle: logger levels, log lines, and the
mirror file the app tails into its own log (#1694, #2220)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import (
    Upstream,
    start_proxy,
    state_path,
    text_of,
    wait_until,
    write_proxy_config,
)
from ._relay import LEGACY_OAUTH, WEBHOOK_ID, WEBHOOK_PATH, relay_config
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")

# Core's own webhook logger: it alone says "Received message for unregistered
# webhook", the line that tells "never arrived" from "arrived, nothing there".
WEBHOOK_LOGGER = logging.getLogger("homeassistant.components.webhook")


@pytest.fixture(autouse=True)
def proxy_logger(component: ModuleType) -> Iterator[logging.Logger]:
    """The component's logger; its level and Core's webhook logger's level are
    process-wide, so each test gets them back as it found them."""
    logger = logging.getLogger(component.__name__)
    saved = (logger.level, WEBHOOK_LOGGER.level)
    yield logger
    logger.setLevel(saved[0])
    WEBHOOK_LOGGER.setLevel(saved[1])


def toggle_config(debug: bool) -> dict[str, object]:
    return {
        "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
        "webhook_id": WEBHOOK_ID,
        "debug_logging": debug,
    }


async def reload_with_debug(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    entry: MockConfigEntry,
    debug: bool,
) -> None:
    """The app rewrites its config and reloads the entry when the toggle flips."""
    write_proxy_config(fs_root, variant, toggle_config(debug))
    assert await hass.config_entries.async_reload(entry.entry_id)


def proxy_messages(caplog: pytest.LogCaptureFixture, logger: logging.Logger) -> str:
    """What the component itself logged (Core and aiohttp log the full path)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name == logger.name)


async def test_toggle_raises_cores_webhook_logger_and_undoes_only_that(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> None:
    """With the toggle on, Core's "unregistered webhook" line becomes visible;
    turning it off puts that logger back."""
    WEBHOOK_LOGGER.setLevel(logging.WARNING)
    entry = await start_proxy(hass, variant, fs_root, toggle_config(True))

    assert WEBHOOK_LOGGER.level == logging.INFO

    await reload_with_debug(hass, variant, fs_root, entry, False)

    assert WEBHOOK_LOGGER.level == logging.NOTSET


async def test_toggle_never_lowers_a_users_webhook_logger_level(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> None:
    """A user who set Core's webhook logger to DEBUG keeps it through an
    on/off cycle of the toggle."""
    WEBHOOK_LOGGER.setLevel(logging.DEBUG)
    entry = await start_proxy(hass, variant, fs_root, toggle_config(True))
    await reload_with_debug(hass, variant, fs_root, entry, False)

    assert WEBHOOK_LOGGER.level == logging.DEBUG


async def test_toggle_on_raises_a_quiet_proxy_logger_to_info(
    hass: HomeAssistant, variant: Variant, fs_root: Path, proxy_logger: logging.Logger
) -> None:
    """Custom components log at WARNING by default, which would hide every
    inbound line the user just asked for."""
    proxy_logger.setLevel(logging.WARNING)

    await start_proxy(hass, variant, fs_root, toggle_config(True))

    assert proxy_logger.level == logging.INFO


async def test_toggle_on_keeps_a_users_debug_level(
    hass: HomeAssistant, variant: Variant, fs_root: Path, proxy_logger: logging.Logger
) -> None:
    proxy_logger.setLevel(logging.DEBUG)

    await start_proxy(hass, variant, fs_root, toggle_config(True))

    assert proxy_logger.level == logging.DEBUG


async def test_toggle_off_undoes_the_info_level_it_raised(
    hass: HomeAssistant, variant: Variant, fs_root: Path, proxy_logger: logging.Logger
) -> None:
    proxy_logger.setLevel(logging.WARNING)
    entry = await start_proxy(hass, variant, fs_root, toggle_config(True))

    await reload_with_debug(hass, variant, fs_root, entry, False)

    assert proxy_logger.level == logging.NOTSET


async def test_toggle_off_keeps_an_info_level_the_user_set(
    hass: HomeAssistant, variant: Variant, fs_root: Path, proxy_logger: logging.Logger
) -> None:
    """With the toggle off (the default) a user's own ``logger:`` INFO is not
    taken for one the proxy raised."""
    proxy_logger.setLevel(logging.INFO)

    await start_proxy(hass, variant, fs_root, toggle_config(False))

    assert proxy_logger.level == logging.INFO


async def post_once(
    hass_client_no_auth: ClientSessionGenerator,
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
) -> int:
    client = await hass_client_no_auth()
    response = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    return response.status


async def test_toggle_on_logs_each_inbound_request_and_its_source(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    """The line proves the request reached Home Assistant, and from where
    (the client address Core validated)."""
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    await post_once(hass_client_no_auth)

    logged = proxy_messages(caplog, proxy_logger)
    assert "[inbound]" in logged
    assert "127.0.0.1" in logged


async def test_toggle_off_logs_no_inbound_lines(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    await start_proxy(hass, variant, fs_root, relay_config(upstream))

    await post_once(hass_client_no_auth)

    assert "[inbound]" not in proxy_messages(caplog, proxy_logger)


async def test_rejected_discovery_probe_is_still_logged(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    """With OAuth on, a client's first unauthenticated probe gets a 401; the
    log still records that it arrived, which is the proof the client reached
    the server."""
    config = relay_config(upstream, debug_logging=True, oauth=LEGACY_OAUTH)
    await start_proxy(hass, variant, fs_root, config)

    status = await post_once(hass_client_no_auth)

    logged = proxy_messages(caplog, proxy_logger)
    assert status == 401
    assert "[inbound]" in logged
    assert "401 Unauthorized" in logged
    assert upstream.requests == []


async def test_toggle_on_logs_the_upstream_answer(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    await post_once(hass_client_no_auth)

    assert "upstream responded 200" in proxy_messages(caplog, proxy_logger)


async def test_inbound_log_says_whether_a_token_came_never_the_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    await post_once(
        hass_client_no_auth,
        headers={"Authorization": "Bearer super-secret-token-value"},
    )

    logged = proxy_messages(caplog, proxy_logger)
    assert "Authorization header: present" in logged
    assert "super-secret-token-value" not in logged


async def test_inbound_log_never_holds_the_request_body(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    await post_once(hass_client_no_auth, body=b'{"secret":"DO-NOT-LOG-THIS-BODY"}')

    logged = proxy_messages(caplog, proxy_logger)
    assert "[inbound]" in logged
    assert "DO-NOT-LOG-THIS-BODY" not in logged


async def test_inbound_log_never_holds_the_full_webhook_id(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    """With OAuth off the webhook id is the shared secret; the log carries
    only enough of it to tell webhooks apart."""
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    await post_once(hass_client_no_auth)

    logged = proxy_messages(caplog, proxy_logger)
    assert "[inbound]" in logged
    assert WEBHOOK_ID not in logged
    assert WEBHOOK_ID[:6] in logged


async def test_inbound_lines_are_mirrored_for_the_app_log(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """The app runs outside Home Assistant; it shows inbound lines in its own
    log by tailing the mirror file the component writes."""
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    await post_once(hass_client_no_auth)
    mirror = state_path(fs_root, variant.inbound_log)
    await wait_until(hass, lambda: "upstream responded" in text_of(mirror))

    mirrored = mirror.read_text()
    assert "[inbound]" in mirrored
    assert "127.0.0.1" in mirrored


async def test_inbound_mirror_stays_capped_and_keeps_the_newest_line(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    component: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long debug session must not grow ``/config`` without bound, and the
    trimming keeps the most recent lines whole."""
    cap = 400
    monkeypatch.setattr(component, "_INBOUND_LOG_CAP", cap)
    statuses = iter([200] * 5 + [201])

    async def answer(request: web.Request) -> web.Response:
        return web.json_response({}, status=next(statuses))

    upstream.handler = answer
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    mirror = state_path(fs_root, variant.inbound_log)
    for _ in range(6):
        await post_once(hass_client_no_auth)
        await hass.async_block_till_done()
    await wait_until(hass, lambda: "upstream responded 201" in text_of(mirror))

    lines = mirror.read_text().splitlines()
    assert mirror.stat().st_size <= cap
    assert all(line.startswith("MCP Proxy [inbound]") for line in lines)
    assert any("upstream responded 201" in line for line in lines)


async def test_unwritable_mirror_never_fails_the_request(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    component: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    proxy_logger: logging.Logger,
) -> None:
    """A read-only or missing ``/config`` only loses the mirror line; the
    write runs in the executor unawaited, so it must swallow its own error."""
    monkeypatch.setattr(component, "INBOUND_LOG_FILE", fs_root / "gone" / "in.log")
    await start_proxy(
        hass, variant, fs_root, relay_config(upstream, debug_logging=True)
    )

    status = await post_once(hass_client_no_auth)
    await wait_until(
        hass,
        lambda: "inbound mirror write failed" in proxy_messages(caplog, proxy_logger),
    )

    assert status == 200
