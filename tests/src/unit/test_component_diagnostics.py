"""Unit tests for the component's "Download diagnostics" handler (#2584).

The file is downloaded from Core's diagnostics integration, so it holds what a
user attaches: Core's own sections (its ``issues`` field among them) around the
component's summary. Startup errors are filed by a real failed bring-up into
Core's issue registry, so the handler reads the shape bring-up actually writes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from http import HTTPStatus
from typing import Any, Literal, get_args, get_type_hints
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.registry import SAVE_DELAY_LONG
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.embedded_server as eserver
import custom_components.ha_mcp_tools.embedded_setup as esetup
from custom_components.ha_mcp_tools.const import (
    DATA_ACCESS_ENABLED,
    DATA_ACCESS_TOKEN,
    DATA_DCR_SIGNING_KEY,
    DATA_LAST_PIP_SPEC,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DATA_WEBHOOK_ID,
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_PIP_SPEC,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.embedded_server import (
    EmbeddedServerError,
    EmbeddedServerManager,
)

# Bound before the autouse ``server`` fixture replaces the module attribute.
from custom_components.ha_mcp_tools.embedded_setup import async_bring_up_server

from ._server_entry import add_server_entry, replace_server, set_up

type FailureKind = Literal["package", "start", "token"]

# Every failure kind bring-up can file, except "token": that one files the
# token repair, which carries the refusal reason rather than an error detail
# and which Core's ``issues`` field already names.
DETAIL_KINDS: list[FailureKind] = [
    kind
    for kind in get_args(get_type_hints(EmbeddedServerError.__init__)["kind"])
    if kind != "token"
]

# One distinct value per secret key, so a leak names the key that leaked.
SECRET_DATA = {
    key: f"secret-{key}-value"
    for key in (
        DATA_WEBHOOK_ID,
        DATA_SECRET_PATH,
        DATA_OAUTH_CLIENT_ID,
        DATA_OAUTH_CLIENT_SECRET,
        DATA_OAUTH_SIGNING_KEY,
        DATA_DCR_SIGNING_KEY,
        DATA_SERVER_USER_ID,
        DATA_REFRESH_TOKEN_ID,
        DATA_ACCESS_TOKEN,
        DATA_LAST_PIP_SPEC,
    )
}
SECRET_OPTIONS = {
    key: f"secret-{key}-value"
    for key in (
        OPT_OAUTH_CLIENT_ID,
        OPT_OAUTH_CLIENT_SECRET,
        OPT_PIP_SPEC,
        OPT_SERVER_URL,
        OPT_EXTERNAL_URL,
        OPT_WEBHOOK_ID_OVERRIDE,
        OPT_SECRET_PATH_OVERRIDE,
    )
}


@pytest.fixture(autouse=True)
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    return replace_server(monkeypatch)


async def _setup_entry(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(data or {}),
        options=dict(options or {}),
        disabled_by=None,
    )
    entry.add_to_hass(hass)
    assert await set_up(hass, entry)
    return entry


async def _setup_server_entry(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = add_server_entry(hass, data=data, options=options)
    assert await set_up(hass, entry)
    return entry


@pytest.fixture(autouse=True)
def installed_version_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def _version(preferred_dist: Any = None) -> str:
        calls.append(preferred_dist)
        return "8.7.0"

    monkeypatch.setattr(eserver, "_installed_ha_mcp_version", _version)
    return calls


@pytest.fixture(autouse=True)
def paired_spec(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The manifest pin the effective spec falls back to; set ``[0]``."""
    pin: list[str | None] = ["ha-mcp==8.7.0"]

    async def _paired(hass: HomeAssistant) -> str | None:
        return pin[0]

    monkeypatch.setattr(eserver, "_async_paired_server_requirement", _paired)
    return pin


async def _fail_bring_up(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entry: MockConfigEntry,
    kind: FailureKind,
    detail: str,
) -> None:
    """Run the entry's real bring-up with the server start failing as ``detail``."""
    monkeypatch.setattr(
        EmbeddedServerManager,
        "async_start",
        AsyncMock(side_effect=EmbeddedServerError(detail, kind=kind)),
    )
    await async_bring_up_server(hass, entry)


async def _download(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, entry: MockConfigEntry
) -> dict[str, Any]:
    """The file "Download diagnostics" serves for ``entry``, as Core builds it."""
    assert await async_setup_component(hass, "diagnostics", {})
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == HTTPStatus.OK
    body: dict[str, Any] = await response.json()
    return body


@pytest.mark.parametrize("kind", DETAIL_KINDS)
@pytest.mark.asyncio
async def test_every_failure_kind_reaches_the_file(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    kind: FailureKind,
) -> None:
    """A failure kind added to bring-up without the reader would drop its
    error from the file."""
    entry = await _setup_server_entry(hass)
    await _fail_bring_up(hass, monkeypatch, entry, kind, "boom")

    result = await _download(hass, hass_client, entry)

    [filed] = [issue["issue_id"] for issue in result["issues"]]
    assert result["data"]["server"]["startup_error"] == {
        "issue_id": filed,
        "detail": "boom",
    }


@pytest.mark.parametrize("kind", DETAIL_KINDS)
@pytest.mark.asyncio
async def test_unscrubbed_detail_stays_out_of_cores_issues_field(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    kind: FailureKind,
) -> None:
    """Core's ``issues`` field carries the data and placeholders of a
    persistent issue, which would put the unscrubbed detail beside the
    scrubbed copy."""
    entry = await _setup_server_entry(hass, data={DATA_ACCESS_TOKEN: "tok-abc"})
    await _fail_bring_up(hass, monkeypatch, entry, kind, "failed with tok-abc")

    result = await _download(hass, hass_client, entry)

    assert result["issues"]
    assert "tok-abc" not in json.dumps(result["issues"])


@pytest.mark.parametrize(
    ("options", "data", "secrets"),
    [
        (None, SECRET_DATA, SECRET_DATA),
        (SECRET_OPTIONS, None, SECRET_OPTIONS),
    ],
    ids=["stored-data", "stored-options"],
)
@pytest.mark.asyncio
async def test_no_secret_value_appears_in_the_file(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any] | None,
    data: dict[str, Any] | None,
    secrets: dict[str, Any],
) -> None:
    """Every stored secret is set and echoed in the startup error; none of the
    values may reach the downloaded file.

    Stored data keys and option overrides are verified separately so overrides
    do not mask unscrubbed data keys.
    """
    entry = await _setup_server_entry(hass, options=options, data=data)
    every_secret = [
        value
        for key, value in (*entry.data.items(), *entry.options.items())
        if key in secrets and isinstance(value, str) and value
    ]
    await _fail_bring_up(
        hass, monkeypatch, entry, "start", "start failed: " + " | ".join(every_secret)
    )

    result = await _download(hass, hass_client, entry)
    serialized = json.dumps(result)

    leaked = [value for value in every_secret if value in serialized]
    assert leaked == []
    startup_error = result["data"]["server"]["startup_error"]
    assert startup_error["issue_id"] == ISSUE_START_FAILED
    assert startup_error["detail"].startswith("start failed: ")
    assert "**REDACTED**" in startup_error["detail"]


@pytest.mark.asyncio
async def test_padded_override_is_scrubbed_in_its_stripped_form(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options keep the override as typed, but the server strips it before
    it reaches the error message."""
    entry = await _setup_server_entry(hass, {OPT_PIP_SPEC: " tok-abc.tgz "})
    await _fail_bring_up(
        hass,
        monkeypatch,
        entry,
        "package",
        "Could not install the server ('tok-abc.tgz')",
    )

    result = await _download(hass, hass_client, entry)

    assert "tok-abc" not in json.dumps(result)


@pytest.mark.parametrize(
    ("url", "scrubbed"),
    [
        (
            "https://user:p@ss@w@pypi.example/simple",
            "https://**REDACTED**@pypi.example/simple",
        ),
        ("https://user:p?ss@host/x.whl", "https://**REDACTED**@host/x.whl"),
    ],
    ids=["raw-at-in-password", "raw-question-mark-in-password"],
)
@pytest.mark.asyncio
async def test_url_credentials_are_scrubbed_even_when_not_stored(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    scrubbed: str,
) -> None:
    """pip echoes index URLs from its own configuration, which never reach the
    entry. A raw ``@`` or ``?`` in the password must not leave part of it."""
    entry = await _setup_server_entry(hass)
    await _fail_bring_up(hass, monkeypatch, entry, "package", f"index {url} failed")

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"]["startup_error"]["detail"] == (
        f"index {scrubbed} failed"
    )


@pytest.mark.asyncio
async def test_secret_containing_another_is_replaced_whole(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that contains a shorter secret is replaced as one piece, so no
    remainder of the longer secret survives around the shorter one's marker.

    Four nested values: replacing in any order but longest first fails here.
    """
    entry = await _setup_server_entry(
        hass,
        {OPT_PIP_SPEC: "x-tarball-tok-abc.tgz", OPT_SERVER_URL: "tarball-tok-abc"},
        {DATA_ACCESS_TOKEN: "tok-abc", DATA_REFRESH_TOKEN_ID: "tok"},
    )
    await _fail_bring_up(
        hass, monkeypatch, entry, "package", "pip failed for x-tarball-tok-abc.tgz"
    )

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"]["startup_error"] == {
        "issue_id": ISSUE_PACKAGE_FAILED,
        "detail": "pip failed for **REDACTED**",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["ha-mcp", "ha-mcp-dev==8.7.0"])
async def test_plain_server_pin_is_not_scrubbed(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    spec: str,
) -> None:
    """``last_pip_spec`` holds a plain server pin (bare or ``==``-pinned) when
    no override is set; it is not a secret, so the error keeps it."""
    entry = await _setup_server_entry(hass, data={DATA_LAST_PIP_SPEC: spec})
    await _fail_bring_up(
        hass, monkeypatch, entry, "package", f"Could not install {spec}"
    )

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"]["startup_error"]["detail"] == (
        f"Could not install {spec}"
    )


@pytest.mark.parametrize(
    "override",
    [
        "ha-mcp==8.7.0 --index-url https://pypi.example/simple?token=tok.abc",
        "ha-mcp==8.7.0+tok.abc",
    ],
    ids=["trailing-option", "local-version-label"],
)
@pytest.mark.asyncio
async def test_override_that_starts_like_a_server_pin_is_scrubbed(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    """Only a whole server pin with a public version is kept: an override
    that merely starts with one carries whatever was typed after it (a local
    version label takes any text), and the install error quotes the spec
    whole."""
    entry = await _setup_server_entry(hass, {OPT_PIP_SPEC: override})
    await _fail_bring_up(
        hass,
        monkeypatch,
        entry,
        "package",
        f"Could not install the server ({override!r}): ...",
    )

    result = await _download(hass, hass_client, entry)

    assert "tok.abc" not in json.dumps(result)


@pytest.mark.asyncio
async def test_version_too_long_to_parse_does_not_break_the_download(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digit run past int()'s limit makes ``Version`` raise ValueError rather
    than InvalidVersion; the override is then scrubbed like any other."""
    override = "ha-mcp==1" + "0" * 5000
    entry = await _setup_server_entry(hass, {OPT_PIP_SPEC: override})
    await _fail_bring_up(
        hass,
        monkeypatch,
        entry,
        "package",
        f"Could not install the server ({override!r}): x",
    )

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"]["startup_error"]["detail"] == (
        "Could not install the server ('**REDACTED**'): x"
    )


@pytest.mark.asyncio
async def test_override_is_scrubbed_in_its_quoted_form(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The install error quotes the spec with ``repr()``, which doubles a
    backslash, so the stored value no longer appears in the text as typed."""
    override = "https://host/pkg.tgz?sig=tok\\abc"
    entry = await _setup_server_entry(hass, {OPT_PIP_SPEC: override})
    await _fail_bring_up(
        hass,
        monkeypatch,
        entry,
        "package",
        f"Could not install the server ({override!r}): x",
    )

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"]["startup_error"]["detail"] == (
        "Could not install the server ('**REDACTED**'): x"
    )


@pytest.mark.asyncio
async def test_issue_restored_after_restart_is_not_reported(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    issue_registry: ir.IssueRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core restores a non-persistent issue inactive and without data or
    placeholders; it describes no current attempt."""
    entry = await _setup_server_entry(hass)
    await _fail_bring_up(hass, monkeypatch, entry, "start", "start failed: something")
    # Core writes the registry to storage after its save delay, then reads it
    # back as it does at the next start.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY_LONG))
    await hass.async_block_till_done()
    stored = hass_storage[ir.STORAGE_KEY]["data"]["issues"]
    assert ISSUE_START_FAILED in [issue["issue_id"] for issue in stored]
    await ir.async_load(hass)
    restored = issue_registry.async_get_issue(DOMAIN, ISSUE_START_FAILED)
    assert restored is not None
    assert restored.active is False

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"]["startup_error"] is None


@pytest.mark.asyncio
async def test_server_summary_reports_configured_options(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Each configured option reaches the file."""
    options = {
        OPT_PIP_SPEC: "ha-mcp==8.6.0",
        OPT_WEBHOOK_AUTH: "ha_auth",
        OPT_ENABLE_WEBHOOK: False,
        OPT_EXTERNAL_URL: "https://ha.example.com",
    }

    entry = await _setup_server_entry(hass, options, {DATA_ACCESS_ENABLED: False})

    result = await _download(hass, hass_client, entry)

    assert result["data"]["server"] == {
        "installed_server_version": "8.7.0",
        "package_override_set": True,
        "access_enabled": False,
        "running": False,
        "bring_up_in_progress": False,
        "startup_error": None,
        "webhook_enabled": False,
        "webhook_auth": "ha_auth",
        "external_url_set": True,
    }


@pytest.mark.parametrize(
    ("pin", "pip_spec", "first"),
    [
        ("ha-mcp-dev==8.8.0.dev1", DEFAULT_PIP_SPEC, DIST_NAME_DEV),
        ("ha-mcp-dev==8.8.0.dev1", "", DIST_NAME_DEV),
        (None, "", DIST_NAME_STABLE),
        ("ha-mcp==8.7.0", "HA_MCP_Dev==8.7.0", DIST_NAME_DEV),
    ],
    ids=["default-spec-is-no-override", "pin", "no-pin", "override-wins"],
)
@pytest.mark.asyncio
async def test_installed_version_reads_the_installing_distribution_first(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    installed_version_calls: list[Any],
    paired_spec: list[str | None],
    pin: str | None,
    pip_spec: str,
    first: str,
) -> None:
    """Read as bring-up reads it (#2765): the distribution the effective spec
    installs goes first, so metadata another one left behind cannot mask it.
    The default spec is no override; without a manifest pin ``ha-mcp`` is read
    first."""
    paired_spec[0] = pin
    entry = await _setup_server_entry(hass, {OPT_PIP_SPEC: pip_spec})

    await _download(hass, hass_client, entry)

    assert installed_version_calls == [first]


@pytest.mark.parametrize("pip_spec", [None, "", DEFAULT_PIP_SPEC])
@pytest.mark.asyncio
async def test_server_summary_defaults_for_unset_options(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, pip_spec: str | None
) -> None:
    """Unset options read as the defaults bring-up applies; an empty pip spec
    or the default spec in the field is not an override."""
    options = {} if pip_spec is None else {OPT_PIP_SPEC: pip_spec}
    entry = await _setup_server_entry(hass, options)

    server = (await _download(hass, hass_client, entry))["data"]["server"]

    assert server["package_override_set"] is False
    assert server["access_enabled"] is True
    assert server["webhook_enabled"] is True
    assert server["webhook_auth"] == WEBHOOK_AUTH_NONE
    assert server["external_url_set"] is False


@pytest.mark.parametrize(
    ("alive", "finished"), [(True, False), (False, True)], ids=["up", "crashed"]
)
@pytest.mark.asyncio
async def test_running_and_bring_up_follow_the_live_objects(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    alive: bool,
    finished: bool,
) -> None:
    """The manager and the finished bring-up task stay in ``hass.data`` after
    the server thread dies, so both flags must read their live state.

    "up": the worker thread is alive while bring-up still waits for it to be
    ready. "crashed": bring-up finished and the thread is gone.
    """
    release = asyncio.Event()

    async def _start(manager: EmbeddedServerManager) -> None:
        await release.wait()

    if finished:
        release.set()
    monkeypatch.setattr(EmbeddedServerManager, "async_start", _start)
    if alive:
        monkeypatch.setattr(
            EmbeddedServerManager, "is_running", property(lambda manager: True)
        )
    monkeypatch.setattr(esetup, "async_bring_up_server", async_bring_up_server)
    monkeypatch.setattr(esetup, "async_register_webhook", AsyncMock(return_value=False))
    monkeypatch.setattr(esetup, "async_register_llm_api", AsyncMock())

    entry = await _setup_server_entry(hass)
    try:
        if finished:
            await hass.async_block_till_done(wait_background_tasks=True)
        server = (await _download(hass, hass_client, entry))["data"]["server"]
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert server["running"] is alive
    assert server["bring_up_in_progress"] is not finished


@pytest.mark.asyncio
async def test_tools_only_install_lists_entries_without_server(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A tools entry predating the marker has no ``entry_type``; it reads as
    the tools entry, and the summary carries no server section."""
    entry = await _setup_entry(hass, {})

    result = await _download(hass, hass_client, entry)

    assert result["data"] == {
        "entries": [{"entry_type": "tools", "state": "loaded", "disabled_by": None}],
        "server": None,
    }


@pytest.mark.asyncio
async def test_server_entry_listed_after_the_tools_entry_is_found(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The tools entry's download describes a server entry listed after it."""
    tools_entry = await _setup_entry(hass, {})
    await _setup_server_entry(hass)

    result = await _download(hass, hass_client, tools_entry)

    assert [e["entry_type"] for e in result["data"]["entries"]] == ["tools", "server"]
    assert result["data"]["server"] is not None
