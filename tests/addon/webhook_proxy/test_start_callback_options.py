"""The none-mode OAuth callback options, from the app's options to the proxy
config the component enforces.

The stable flavor has no such options until a promote brings them; there the
options do nothing and the config file keeps its earlier shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import submodule
from ._proxy import Upstream, set_up_proxy, write_proxy_config
from ._start import App, CoreLink, connect_core, make_app, run_main
from ._variants import Variant, callback_allowlist_supported

ALLOWLIST = "oauth_redirect_allowlist"
ALLOW_ANY = "oauth_allow_any_callback"


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    app = make_app(variant, fs_root, monkeypatch)
    app.deploy_component()
    return app


@pytest.fixture
async def core(
    app: App,
    component: ModuleType,
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
) -> CoreLink:
    return await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)


async def test_configured_callbacks_reach_the_component_trimmed_and_deduplicated(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream, variant: Variant
) -> None:
    """A callback typed with stray spaces or twice must still match the
    client's exact redirect, so the component gets each one once, trimmed."""
    app.set_options(
        mcp_server_url=upstream.target_url(),
        oauth_redirect_allowlist=[
            " https://client.example/cb ",
            "https://client.example/cb",
            "http://localhost/callback",
        ],
    )

    assert await run_main(hass, app) == 0

    config = app.proxy_config()
    if not callback_allowlist_supported(variant):
        assert ALLOWLIST not in config
        return
    assert config[ALLOWLIST] == [
        "https://client.example/cb",
        "http://localhost/callback",
    ]


SHAPE_REASON = "must be https"
LIMIT_REASON = "beyond the limit"


@pytest.mark.parametrize(
    ("beyond_limit", "unusable", "reason", "other_reason"),
    [
        (False, "https://user:pw@client.example/cb", SHAPE_REASON, LIMIT_REASON),
        (True, "https://client.example/one-too-many", LIMIT_REASON, SHAPE_REASON),
    ],
    ids=["refused_shape", "beyond_entry_limit"],
)
async def test_unusable_callback_is_dropped_and_named_while_the_rest_apply(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    component: ModuleType,
    beyond_limit: bool,
    unusable: str,
    reason: str,
    other_reason: str,
) -> None:
    """One bad entry must neither stop the app nor take the user's other
    callbacks down with it, and the log must say which entry was ignored and
    why: an entry cut by the limit is not malformed."""
    valid = ["https://client.example/cb"]
    if beyond_limit and callback_allowlist_supported(variant):
        limit = submodule(component, "oauth_redirect_allowlist").MAX_OAUTH_CALLBACKS
        valid = [f"https://client.example/cb{n}" for n in range(limit)]
    app.set_options(
        mcp_server_url=upstream.target_url(),
        oauth_redirect_allowlist=[*valid, unusable],
    )

    assert await run_main(hass, app) == 0

    config = app.proxy_config()
    if not callback_allowlist_supported(variant):
        assert ALLOWLIST not in config
        return
    assert config[ALLOWLIST] == valid
    [line] = [line for line in app.err().splitlines() if repr(unusable) in line]
    assert reason in line
    assert other_reason not in line


async def test_an_emptied_list_reaches_the_component_as_the_whole_list(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream, variant: Variant
) -> None:
    """A saved list replaces the built-in one even when empty, as in the
    HA-MCP integration; dropping it here would quietly bring the built-in
    callbacks back for a user who removed them."""
    app.set_options(mcp_server_url=upstream.target_url(), oauth_redirect_allowlist=[])

    assert await run_main(hass, app) == 0

    config = app.proxy_config()
    if not callback_allowlist_supported(variant):
        assert ALLOWLIST not in config
        return
    assert config[ALLOWLIST] == []


async def test_allow_any_callback_reaches_the_component(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream, variant: Variant
) -> None:
    """The opt-out must reach the component, or a client whose callback is not
    listed stays locked out after the user turned the list off."""
    app.set_options(mcp_server_url=upstream.target_url(), oauth_allow_any_callback=True)

    assert await run_main(hass, app) == 0

    config = app.proxy_config()
    if not callback_allowlist_supported(variant):
        assert ALLOW_ANY not in config
        return
    assert config[ALLOW_ANY] is True


@pytest.mark.parametrize(
    ("options", "summary"),
    [
        ({}, ": the built-in list"),
        ({ALLOWLIST: ["https://client.example/cb"]}, ": 1 configured callback"),
        ({ALLOWLIST: []}, ": none (the configured list is empty"),
        (
            {ALLOWLIST: ["https://u:p@client.example/a", "ftp://client.example/b"]},
            ": none (all 2 configured entries were ignored",
        ),
        ({ALLOWLIST: [], ALLOW_ANY: True}, ": any (Allow any OAuth callback URL is on"),
    ],
    ids=["default", "configured", "emptied", "all_ignored", "allow_any"],
)
async def test_startup_summary_says_which_callbacks_can_sign_in(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    options: dict[str, object],
    summary: str,
) -> None:
    """With OAuth off, the log tells the user which callbacks may complete
    sign-in, so a client sent to the error page can be traced to the list."""
    app.set_options(mcp_server_url=upstream.target_url(), **options)

    assert await run_main(hass, app) == 0

    lines = [line for line in app.info_lines() if "OAuth callbacks allowed" in line]
    if not callback_allowlist_supported(variant):
        assert lines == []
        return
    assert len(lines) == 1
    assert summary in lines[0]


async def test_summary_after_an_update_says_the_list_waits_for_the_restart(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    fs_root: Path,
) -> None:
    """After an app update Core keeps running the previous component until
    Home Assistant restarts, and one from before the list redirects to any
    callback; the summary must not claim the list is already in force."""
    write_proxy_config(
        fs_root,
        variant,
        {"target_url": upstream.target_url(), "webhook_id": "mcp_previous_run"},
    )
    await set_up_proxy(hass, variant)
    manifest_file = app.deployed_component / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest_file.write_text(json.dumps({**manifest, "version": "0.0.1"}))
    app.set_options(mcp_server_url=upstream.target_url())

    assert await run_main(hass, app) == 0

    lines = [line for line in app.info_lines() if "OAuth callbacks allowed" in line]
    if not callback_allowlist_supported(variant):
        assert lines == []
        return
    [line] = lines
    assert "once Home Assistant is restarted" in line


@pytest.mark.parametrize(
    "option",
    [{ALLOWLIST: ["https://client.example/cb"]}, {ALLOW_ANY: True}],
    ids=["allowlist", "allow_any"],
)
async def test_callback_options_with_oauth_on_say_they_do_nothing(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    option: dict[str, object],
) -> None:
    """The callback options only govern none mode; with Enable OAuth on, a
    user who set one must be told it has no effect rather than trust it."""
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True, **option)

    assert await run_main(hass, app) == 0

    lines = [
        line
        for line in app.err().splitlines()
        if "only while Enable OAuth is off" in line
    ]
    if not callback_allowlist_supported(variant):
        assert lines == []
        return
    assert len(lines) == 1
