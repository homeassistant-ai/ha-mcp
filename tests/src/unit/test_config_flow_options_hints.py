"""The prose around the server options form: versions, tools module, URLs, panel link.

The hints are assembled from live state when the form opens, and a failed read
must degrade to safe text rather than take the form down.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import Integration, IntegrationNotFound, async_get_integration

from custom_components.ha_mcp_tools import config_flow, embedded_setup
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_LOOPBACK,
    CONF_ENTRY_TYPE,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ENTRY_TYPE_TOOLS,
    OPT_BIND_HOST,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
)

from ._config_flow_core import (
    MANIFEST_VERSION,
    WEBHOOK_ID,
    add_options_entry,
    add_tools_entry,
    open_options_form,
    placeholders,
    start_options_flow,
)

PAIRED = "(installed with this component release)"
WITH_SECRET = {DATA_WEBHOOK_ID: WEBHOOK_ID, DATA_SECRET_PATH: "/private_x"}


async def _versions(
    hass: HomeAssistant, *, options: dict[str, Any] | None = None
) -> str:
    return placeholders(await open_options_form(hass, options=options))["versions"]


async def _connect_url(hass: HomeAssistant, **options: Any) -> str:
    form = await open_options_form(hass, data=WITH_SECRET, options=options)
    return placeholders(form)["connect_url"]


async def test_versions_names_the_manifest_and_the_installed_server(
    hass: HomeAssistant,
) -> None:
    """The Configure form says which component and server version are installed."""
    versions = await _versions(hass)

    assert re.match(
        rf"Component {re.escape(MANIFEST_VERSION)} - Server ha-mcp \d+\.\d+\.\d+ "
        rf"\({re.escape(PAIRED[1:-1])}\)",
        versions,
    )


async def test_versions_degrade_to_safe_text_when_both_reads_fail(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken version read must not break the form, and says so in the log.

    Both degradations are logged at warning level: they put a visible fallback
    on the form, and a debug-level log would leave them unnoticed.
    """
    monkeypatch.setattr(
        config_flow,
        "async_get_integration",
        AsyncMock(side_effect=IntegrationNotFound(DOMAIN)),
    )
    monkeypatch.setattr(
        importlib.metadata, "version", Mock(side_effect=OSError("metadata boom"))
    )

    with caplog.at_level(logging.WARNING):
        versions = await _versions(hass)

    assert versions.startswith(
        f"Component unknown - Server ha-mcp not installed yet {PAIRED}"
    )
    assert "Could not read the component version" in caplog.text
    assert "Could not read the server version" in caplog.text


async def test_versions_say_when_the_server_is_not_installed_yet(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the server package is installed its half reads "not installed yet"."""
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        Mock(side_effect=importlib.metadata.PackageNotFoundError("ha-mcp")),
    )

    versions = await _versions(hass)

    assert versions.startswith(
        f"Component {MANIFEST_VERSION} - Server ha-mcp not installed yet {PAIRED}"
    )


async def test_a_manifest_without_a_version_shows_the_unknown_wording(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing manifest version reads "unknown", never the text "None"."""
    real = await async_get_integration(hass, DOMAIN)
    versionless = Integration(
        hass,
        real.pkg_path,
        real.file_path,
        {"domain": DOMAIN, "name": real.name, "requirements": []},
    )
    monkeypatch.setattr(
        config_flow, "async_get_integration", AsyncMock(return_value=versionless)
    )

    with caplog.at_level(logging.WARNING):
        versions = await _versions(hass)

    assert versions.startswith("Component unknown - ")
    assert "None" not in versions
    assert "carries no version" in caplog.text


async def test_a_pip_override_is_named_as_the_server_source(
    hass: HomeAssistant,
) -> None:
    """The version line tells an override apart from the server this release pins."""
    versions = await _versions(hass, options={OPT_PIP_SPEC: "ha-mcp==0.0.1"})

    assert "(pip requirement override)" in versions
    assert PAIRED not in versions


async def test_missing_tools_entry_is_flagged_under_the_versions(
    hass: HomeAssistant,
) -> None:
    """A server-only install is told the file and YAML tools need the second entry.

    Users otherwise learn about it only when a tool call fails (#1996).
    """
    versions = await _versions(hass)

    assert "Not installed" in versions
    assert "Add entry" in versions


@pytest.mark.parametrize(
    "tools_data",
    [{CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS}, {}],
    ids=["typed", "legacy-without-entry-type"],
)
async def test_loaded_tools_entry_reads_installed(
    hass: HomeAssistant, tools_data: dict[str, str]
) -> None:
    """A loaded tools entry reads Installed, also one stored before entry types."""
    await add_tools_entry(hass, data=tools_data, loaded=True)

    versions = await _versions(hass)

    assert "tools module (optional): Installed" in versions
    assert "Not installed" not in versions
    assert "not loaded" not in versions


async def test_unloaded_tools_entry_points_at_enabling_it(
    hass: HomeAssistant,
) -> None:
    """A tools entry that is not loaded serves no services, so it is not "Installed"."""
    await add_tools_entry(hass)

    versions = await _versions(hass)

    assert "not loaded" in versions
    assert "enable or reload" in versions


async def test_one_loaded_tools_entry_is_enough_for_installed(
    hass: HomeAssistant,
) -> None:
    """An unloaded sibling does not downgrade a loaded tools entry."""
    await add_tools_entry(hass)
    await add_tools_entry(hass, loaded=True)

    versions = await _versions(hass)

    assert "tools module (optional): Installed" in versions
    assert "not loaded" not in versions


async def test_unreadable_entry_registry_drops_the_tools_line_and_logs_it(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing entry read drops the tools line, keeps the version line, and warns."""
    entry = add_options_entry(hass)
    opened = await start_options_flow(hass, entry)
    hass.config_entries.options.async_abort(opened["flow_id"])
    with monkeypatch.context() as patched, caplog.at_level(logging.WARNING):
        patched.setattr(
            hass.config_entries, "async_entries", Mock(side_effect=RuntimeError("boom"))
        )
        form = await start_options_flow(hass, entry)

    versions = placeholders(form)["versions"]
    assert versions.startswith(f"Component {MANIFEST_VERSION} - ")
    assert "tools module" not in versions
    assert "Could not read the tools-entry state" in caplog.text


async def test_panel_hint_links_the_panel_while_the_sidebar_is_on(
    hass: HomeAssistant,
) -> None:
    """The panel sentence names the panel URL; an absent option counts as on."""
    hint = placeholders(await open_options_form(hass))["panel_hint"]

    assert "(/ha-mcp)" in hint


async def test_panel_hint_is_empty_while_the_sidebar_is_off(
    hass: HomeAssistant,
) -> None:
    """With the panel off the link would 404, so the sentence is left out."""
    form = await open_options_form(hass, options={OPT_ENABLE_SIDEBAR_PANEL: False})

    assert placeholders(form)["panel_hint"] == ""


async def test_connect_urls_use_the_configured_port(hass: HomeAssistant) -> None:
    """The direct-access URL carries the configured port, not the default."""
    hint = await _connect_url(hass, **{OPT_SERVER_PORT: 9999})

    assert "/api/webhook/mcp_abc" in hint
    assert ":9999/private_x" in hint
    assert f":{DEFAULT_SERVER_PORT}/" not in hint


async def test_connect_urls_before_the_first_start_point_at_the_log(
    hass: HomeAssistant,
) -> None:
    """With no webhook id minted yet the form says the URLs appear once it starts."""
    form = await open_options_form(hass, data={})

    hint = placeholders(form)["connect_url"].lower()

    assert "once the server has started" in hint
    assert "notification" not in hint


@pytest.mark.parametrize("webhook_enabled", [True, False])
async def test_connect_urls_list_the_resolved_urls(
    hass: HomeAssistant, webhook_enabled: bool
) -> None:
    """The Configure screen lists the real URLs, and the webhook only while it is on."""
    hint = await _connect_url(
        hass,
        **{
            OPT_ENABLE_WEBHOOK: webhook_enabled,
            OPT_EXTERNAL_URL: "https://example.duckdns.org",
        },
    )

    assert "http://10.10.10.10:9584/private_x (direct access)" in hint
    assert "<your-home-assistant-url>" not in hint
    assert (
        "https://example.duckdns.org/api/webhook/mcp_abc" in hint
    ) is webhook_enabled


@pytest.mark.parametrize("resolver", ["raises", "finds_nothing"])
async def test_connect_urls_fall_back_to_the_placeholder_form(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, resolver: str
) -> None:
    """A resolver that fails or finds nothing must not take the form down.

    The hint degrades to the placeholder form instead of an empty header.
    """

    def build(*args: object, **kwargs: object) -> list[str]:
        if resolver == "raises":
            raise RuntimeError("resolution boom")
        return []

    monkeypatch.setattr(embedded_setup, "build_connect_urls", build)

    hint = await _connect_url(hass)

    assert "<your-home-assistant-url>" in hint
    assert "/api/webhook/mcp_abc" in hint


async def test_local_only_mode_never_shows_a_webhook_url(hass: HomeAssistant) -> None:
    """With the webhook off and nothing resolvable the hint is the loopback URL.

    The webhook endpoint is never registered then, so a webhook URL would 404.
    """
    hint = await _connect_url(
        hass,
        **{
            OPT_ENABLE_WEBHOOK: False,
            OPT_SERVER_PORT: 9999,
            OPT_BIND_HOST: BIND_HOST_LOOPBACK,
        },
    )

    assert hint.startswith("Remote access via webhook is disabled")
    assert "http://127.0.0.1:9999/private_x" in hint
    assert "/api/webhook/" not in hint


async def test_local_only_mode_survives_a_failing_resolver(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver error must not bring back a webhook URL that 404s."""

    def boom(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("resolution boom")

    monkeypatch.setattr(embedded_setup, "build_connect_urls", boom)

    hint = await _connect_url(hass, **{OPT_ENABLE_WEBHOOK: False})

    assert hint.startswith("Remote access via webhook is disabled")
    assert f"http://127.0.0.1:{DEFAULT_SERVER_PORT}/private_x" in hint
    assert "/api/webhook/" not in hint
