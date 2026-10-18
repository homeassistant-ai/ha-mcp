"""Unit tests for the component's "Download diagnostics" handler (#2584).

Home Assistant is stubbed via ``_embedded_stubs``. Startup errors are filed
through the real ``embedded_setup._create_issue`` into a recording issue
registry, so the handler reads the placeholder shape bring-up actually writes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.diagnostics as diag  # noqa: E402
import custom_components.ha_mcp_tools.embedded_server as eserver  # noqa: E402
import custom_components.ha_mcp_tools.embedded_setup as esetup  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CONF_ENTRY_TYPE,
    DATA_ACCESS_TOKEN,
    DATA_BRINGUP_TASK,
    DATA_DCR_SIGNING_KEY,
    DATA_LAST_PIP_SPEC,
    DATA_MANAGER,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DATA_WEBHOOK_ID,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_CHANNEL,
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_AUTO_UPDATE,
    OPT_CHANNEL,
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


class RecordingIssueRegistry:
    """Keeps what ``ir.async_create_issue`` files; serves it back like Core."""

    def __init__(self) -> None:
        self.issues: dict[str, Any] = {}

    def async_create_issue(
        self, hass: Any, domain: str, issue_id: str, **kwargs: Any
    ) -> None:
        assert domain == DOMAIN
        self.issues[issue_id] = SimpleNamespace(
            active=True,
            is_persistent=kwargs.get("is_persistent", False),
            translation_placeholders=kwargs["translation_placeholders"],
        )

    def async_get_issue(self, domain: str, issue_id: str) -> Any:
        assert domain == DOMAIN
        return self.issues.get(issue_id)


class FakeHass:
    def __init__(self, entries: list[Any], data: dict[str, Any] | None = None):
        self.data = {DOMAIN: dict(data or {})}
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: list(entries) if domain == DOMAIN else []
        )

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _entry(data: dict[str, Any], options: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        data=data,
        options=dict(options or {}),
        state=SimpleNamespace(value="loaded"),
        disabled_by=None,
    )


def _server_entry(
    options: dict[str, Any] | None = None, data: dict[str, Any] | None = None
) -> Any:
    return _entry({CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER, **(data or {})}, options)


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> RecordingIssueRegistry:
    """One registry behind both the producer and the handler.

    Autouse: the stubbed ``ir`` would otherwise hand back a MagicMock issue.
    """
    reg = RecordingIssueRegistry()
    fake_ir = SimpleNamespace(
        async_get=lambda hass: reg,
        async_create_issue=reg.async_create_issue,
        IssueSeverity=SimpleNamespace(ERROR="error"),
    )
    monkeypatch.setattr(diag, "ir", fake_ir)
    monkeypatch.setattr(esetup, "ir", fake_ir)
    return reg


@pytest.fixture(autouse=True)
def installed_version_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def _version(preferred_dist: Any = None) -> str:
        calls.append(preferred_dist)
        return "8.7.0"

    monkeypatch.setattr(eserver, "_installed_ha_mcp_version", _version)
    return calls


def _fail_bring_up(kind: str, detail: str) -> None:
    """File the repair issue exactly as a failed bring-up does."""
    esetup._create_issue(None, kind, detail)  # type: ignore[arg-type]


async def _diagnostics(hass: FakeHass) -> dict[str, Any]:
    # The handler ignores which entry was asked for: both get the same summary.
    return await diag.async_get_config_entry_diagnostics(hass, None)  # type: ignore[arg-type]


def test_reads_every_issue_bring_up_files() -> None:
    """A failure kind added to bring-up without the reader would drop its
    error from the file."""
    assert set(diag._STARTUP_ISSUE_IDS) == set(esetup._ISSUE_BY_KIND.values())


def test_unscrubbed_detail_stays_out_of_cores_issues_field(
    registry: RecordingIssueRegistry,
) -> None:
    """Core's ``issues`` field carries the placeholders of a persistent issue,
    which would put the unscrubbed detail beside the scrubbed copy."""
    _fail_bring_up("package", "detail")

    assert registry.issues[ISSUE_PACKAGE_FAILED].is_persistent is False


@pytest.mark.asyncio
async def test_no_secret_value_appears_in_the_file() -> None:
    """Every stored secret is set and echoed in the startup error; none of the
    values may reach the serialized download."""
    every_secret = [*SECRET_DATA.values(), *SECRET_OPTIONS.values()]
    _fail_bring_up("start", "start failed: " + " | ".join(every_secret))
    entry = _server_entry(SECRET_OPTIONS, SECRET_DATA)

    result = await _diagnostics(FakeHass([entry]))
    serialized = json.dumps(result)

    leaked = [value for value in every_secret if value in serialized]
    assert leaked == []
    assert result["server"]["startup_error"]["issue_id"] == ISSUE_START_FAILED
    assert result["server"]["startup_error"]["detail"].startswith("start failed: ")
    assert "**REDACTED**" in result["server"]["startup_error"]["detail"]


@pytest.mark.asyncio
async def test_padded_override_is_scrubbed_in_its_stripped_form() -> None:
    """The options keep the override as typed, but the server strips it before
    it reaches the error message."""
    _fail_bring_up("package", "Could not install the server ('tok-abc.tgz')")
    entry = _server_entry({OPT_PIP_SPEC: " tok-abc.tgz "})

    result = await _diagnostics(FakeHass([entry]))

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
    url: str, scrubbed: str
) -> None:
    """pip echoes index URLs from its own configuration, which never reach the
    entry. A raw ``@`` or ``?`` in the password must not leave part of it."""
    _fail_bring_up("package", f"index {url} failed")

    result = await _diagnostics(FakeHass([_server_entry()]))

    assert result["server"]["startup_error"]["detail"] == f"index {scrubbed} failed"


@pytest.mark.asyncio
async def test_secret_containing_another_is_replaced_whole() -> None:
    """A value that contains a shorter secret is replaced as one piece, so no
    remainder of the longer secret survives around the shorter one's marker.

    Four nested values: replacing in any order but longest first fails here.
    """
    _fail_bring_up("package", "pip failed for x-tarball-tok-abc.tgz")
    entry = _server_entry(
        {OPT_PIP_SPEC: "x-tarball-tok-abc.tgz", OPT_SERVER_URL: "tarball-tok-abc"},
        {DATA_ACCESS_TOKEN: "tok-abc", DATA_REFRESH_TOKEN_ID: "tok"},
    )

    result = await _diagnostics(FakeHass([entry]))

    assert result["server"]["startup_error"] == {
        "issue_id": ISSUE_PACKAGE_FAILED,
        "detail": "pip failed for **REDACTED**",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["ha-mcp", "ha-mcp-dev==8.7.0"])
async def test_plain_channel_spec_is_not_scrubbed(spec: str) -> None:
    """``last_pip_spec`` holds a plain channel spec (bare or ``==``-pinned) when
    no override is set; it is not a secret, so the error keeps it."""
    _fail_bring_up("package", f"Could not install {spec}")
    entry = _server_entry(data={DATA_LAST_PIP_SPEC: spec})

    result = await _diagnostics(FakeHass([entry]))

    assert result["server"]["startup_error"]["detail"] == f"Could not install {spec}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        "ha-mcp==8.7.0 --index-url https://pypi.example/simple?token=tok.abc",
        "ha-mcp==8.7.0+tok.abc",
    ],
    ids=["trailing-option", "local-version-label"],
)
async def test_override_that_starts_like_a_channel_spec_is_scrubbed(
    override: str,
) -> None:
    """Only a whole channel spec with a public version is kept: an override
    that merely starts with one carries whatever was typed after it (a local
    version label takes any text), and the install error quotes the spec
    whole."""
    _fail_bring_up("package", f"Could not install the server ({override!r}): ...")
    entry = _server_entry({OPT_PIP_SPEC: override})

    result = await _diagnostics(FakeHass([entry]))

    assert "tok.abc" not in json.dumps(result)


@pytest.mark.asyncio
async def test_version_too_long_to_parse_does_not_break_the_download() -> None:
    """A digit run past int()'s limit makes ``Version`` raise ValueError rather
    than InvalidVersion; the override is then scrubbed like any other."""
    override = "ha-mcp==1" + "0" * 5000
    _fail_bring_up("package", f"Could not install the server ({override!r}): x")
    entry = _server_entry({OPT_PIP_SPEC: override})

    result = await _diagnostics(FakeHass([entry]))

    assert result["server"]["startup_error"]["detail"] == (
        "Could not install the server ('**REDACTED**'): x"
    )


@pytest.mark.asyncio
async def test_override_is_scrubbed_in_its_quoted_form() -> None:
    """The install error quotes the spec with ``repr()``, which doubles a
    backslash, so the stored value no longer appears in the text as typed."""
    override = "https://host/pkg.tgz?sig=tok\\abc"
    _fail_bring_up("package", f"Could not install the server ({override!r}): x")
    entry = _server_entry({OPT_PIP_SPEC: override})

    result = await _diagnostics(FakeHass([entry]))

    assert result["server"]["startup_error"]["detail"] == (
        "Could not install the server ('**REDACTED**'): x"
    )


@pytest.mark.asyncio
async def test_issue_restored_after_restart_is_not_reported(
    registry: RecordingIssueRegistry,
) -> None:
    """Core restores a non-persistent issue inactive and without placeholders;
    it describes no current attempt."""
    registry.issues[ISSUE_START_FAILED] = SimpleNamespace(
        active=False, translation_placeholders=None
    )

    result = await _diagnostics(FakeHass([_server_entry()]))

    assert result["server"]["startup_error"] is None


@pytest.mark.asyncio
async def test_server_summary_reports_configured_options() -> None:
    """Each configured option reaches the file."""
    options = {
        OPT_CHANNEL: "dev",
        OPT_AUTO_UPDATE: False,
        OPT_PIP_SPEC: "ha-mcp==8.6.0",
        OPT_WEBHOOK_AUTH: "ha_auth",
        OPT_ENABLE_WEBHOOK: False,
        OPT_EXTERNAL_URL: "https://ha.example.com",
    }

    result = await _diagnostics(FakeHass([_server_entry(options)]))

    assert result["server"] == {
        "installed_server_version": "8.7.0",
        "channel": "dev",
        "auto_update": False,
        "package_override_set": True,
        "running": False,
        "bring_up_in_progress": False,
        "startup_error": None,
        "webhook_enabled": False,
        "webhook_auth": "ha_auth",
        "external_url_set": True,
    }


@pytest.mark.parametrize(
    ("channel", "pip_spec", "first"),
    [
        ("dev", DEFAULT_PIP_SPEC, DIST_NAME_DEV),
        ("stable", "HA_MCP_Dev==8.7.0", DIST_NAME_DEV),
        ("dev", "ha-mcp==8.6.0", DIST_NAME_STABLE),
        ("dev", "https://host/pkg.tgz", DIST_NAME_STABLE),
    ],
    ids=[
        "dev-default-spec",
        "override-names-dev",
        "override-names-stable",
        "bare-url-on-dev",
    ],
)
@pytest.mark.asyncio
async def test_installed_version_reads_the_installing_distribution_first(
    installed_version_calls: list[Any], channel: str, pip_spec: str, first: str
) -> None:
    """An override skips removing the other channel's distribution, so its
    metadata can remain and must not mask the installed version. Read as
    bring-up reads an override: stable first unless it names ``ha-mcp-dev``
    (a tarball URL installs ``ha-mcp`` on every channel). The default spec is
    no override."""
    options = {OPT_CHANNEL: channel, OPT_PIP_SPEC: pip_spec}

    await _diagnostics(FakeHass([_server_entry(options)]))

    assert installed_version_calls == [first]


@pytest.mark.parametrize("pip_spec", [None, "", DEFAULT_PIP_SPEC])
@pytest.mark.asyncio
async def test_server_summary_defaults_for_unset_options(pip_spec: Any) -> None:
    """Unset options read as the defaults bring-up applies; an empty pip spec
    or the default spec in the field is not an override."""
    options = {} if pip_spec is None else {OPT_PIP_SPEC: pip_spec}

    server = (await _diagnostics(FakeHass([_server_entry(options)])))["server"]

    assert server["channel"] == DEFAULT_CHANNEL
    assert server["auto_update"] is DEFAULT_AUTO_UPDATE
    assert server["package_override_set"] is False
    assert server["webhook_enabled"] is True
    assert server["webhook_auth"] == WEBHOOK_AUTH_NONE
    assert server["external_url_set"] is False


@pytest.mark.parametrize(
    ("alive", "task_done"), [(True, False), (False, True)], ids=["up", "crashed"]
)
@pytest.mark.asyncio
async def test_running_and_bring_up_follow_the_live_objects(
    monkeypatch: pytest.MonkeyPatch, alive: bool, task_done: bool
) -> None:
    """The manager and the finished bring-up task stay in ``hass.data`` after
    the server thread dies, so both flags must read their live state."""
    monkeypatch.setattr(
        eserver.EmbeddedServerManager, "is_running", property(lambda self: alive)
    )
    manager = object.__new__(eserver.EmbeddedServerManager)
    bringup = SimpleNamespace(done=lambda: task_done)
    hass = FakeHass(
        [_server_entry()], data={DATA_MANAGER: manager, DATA_BRINGUP_TASK: bringup}
    )

    server = (await _diagnostics(hass))["server"]

    assert server["running"] is alive
    assert server["bring_up_in_progress"] is not task_done


@pytest.mark.asyncio
async def test_tools_only_install_lists_entries_without_server() -> None:
    """A tools entry predating the marker has no ``entry_type``; it reads as
    the tools entry, and the summary carries no server section."""
    result = await _diagnostics(FakeHass([_entry({})]))

    assert result == {
        "entries": [{"entry_type": "tools", "state": "loaded", "disabled_by": None}],
        "server": None,
    }


@pytest.mark.asyncio
async def test_server_entry_listed_after_the_tools_entry_is_found() -> None:
    """The tools entry's download describes a server entry listed after it."""
    result = await _diagnostics(FakeHass([_entry({}), _server_entry()]))

    assert [e["entry_type"] for e in result["entries"]] == ["tools", "server"]
    assert result["server"] is not None
