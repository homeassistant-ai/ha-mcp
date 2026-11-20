"""The none-mode OAuth callback options, from the app's options to the proxy
config the component enforces.

The stable flavor has no such options until a promote brings them; there the
options do nothing and the config file keeps its earlier shape.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream
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


@pytest.mark.parametrize(
    ("valid", "unusable"),
    [
        (["https://client.example/cb"], "https://user:pw@client.example/cb"),
        (
            [f"https://client.example/cb{n}" for n in range(50)],
            "https://client.example/one-too-many",
        ),
    ],
    ids=["refused_shape", "beyond_entry_limit"],
)
async def test_unusable_callback_is_dropped_and_named_while_the_rest_apply(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    valid: list[str],
    unusable: str,
) -> None:
    """One bad entry must neither stop the app nor take the user's other
    callbacks down with it, and the log must say which entry was ignored."""
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
    assert repr(unusable) in app.err()


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
        ({}, "the built-in list"),
        ({ALLOWLIST: ["https://client.example/cb"]}, "1 configured"),
        ({ALLOWLIST: [], ALLOW_ANY: True}, "any"),
    ],
    ids=["default", "configured", "allow_any"],
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
