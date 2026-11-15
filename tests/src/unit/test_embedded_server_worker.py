"""The in-process server's worker thread, started for real (#1527, #1890, #1904).

Each test starts ``EmbeddedServerManager`` on a real ``hass`` with a real
administrator token: the worker imports the real ha-mcp, builds the real
server and serves it with the real uvicorn on a free loopback port. What a
test needs from an older or broken ha-mcp it gets from ``ImportHook``, which
acts on the worker's first import of a module; a hooked start gives the entry a
package override, which makes the start import everything again.
"""

from __future__ import annotations

import asyncio
import errno
import importlib
import logging
import os
import socket
from typing import Any

import pytest
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    DATA_ADMIN_TOKEN,
    DATA_SECRET_PATH,
    OPT_ENABLE_LLM_API,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    SERVER_CONFIG_SUBDIR,
)

from ._embedded_server_core import (
    ImportHook,
    http_get,
    new_manager,
    running_server,
    server_options,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import import_hook as import_hook

pytestmark = pytest.mark.usefixtures("embedded_server_env", "socket_enabled")

# An entry with an override always purges, so the hooked imports are cold.
COLD = {OPT_PIP_SPEC: "ha-mcp==99.0.0"}
HA_URL = "http://ha.local:8123"


def server_config() -> Any:
    """The ``ha_mcp.config`` module the worker imported."""
    return importlib.import_module("ha_mcp.config")


async def refuses(port: int) -> bool:
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return True
    writer.close()
    return False


# -- start and stop ----------------------------------------------------------


async def test_a_started_server_listens_until_it_is_stopped(
    hass: HomeAssistant,
) -> None:
    async with running_server(hass) as (mgr, _entry):
        assert mgr.is_running
        assert not await refuses(mgr.port)

    assert not mgr.is_running
    assert await refuses(mgr.port)


async def test_stopping_a_server_that_never_started_is_a_noop(
    hass: HomeAssistant,
) -> None:
    mgr, _entry = await new_manager(hass)

    await mgr.async_stop()

    assert not mgr.is_running


async def test_a_browser_that_opens_the_server_url_sees_the_setup_guidance(
    hass: HomeAssistant,
) -> None:
    async with running_server(hass) as (mgr, entry):
        status, body = await http_get(mgr.port, entry.data[DATA_SECRET_PATH])

    assert status == 405
    assert "HA-MCP server is up and running" in body


async def test_an_older_server_without_the_browser_landing_keeps_serving(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    import_hook.fail(
        "ha_mcp.browser_landing",
        ModuleNotFoundError("No module named 'ha_mcp.browser_landing'", name="x"),
    )
    async with running_server(hass, options=COLD) as (mgr, entry):
        status, body = await http_get(mgr.port, entry.data[DATA_SECRET_PATH])

    assert status in (404, 405)
    assert "HA-MCP server is up and running" not in body


async def test_a_busy_port_stops_the_start_naming_the_cause(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """uvicorn turns a bind failure into ``SystemExit``; without unwrapping it the
    start only reported a readiness timeout (#1904)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        options = {**server_options(), OPT_SERVER_PORT: taken.getsockname()[1]}
        mgr, _entry = await new_manager(hass, token=True, options=options)

        with pytest.raises(es.EmbeddedServerError) as err:
            await mgr.async_start()
        await mgr.async_stop()

    assert err.value.kind == "start"
    assert "exited during startup" in str(err.value)
    assert os.strerror(errno.EADDRINUSE).lower() in str(err.value).lower()
    assert "failed to start" not in str(err.value)


# -- what the worker is handed -----------------------------------------------


async def test_the_worker_sees_its_config_dir_and_no_secret_in_the_environment(
    hass: HomeAssistant,
) -> None:
    """The loopback URL and the token reach ha-mcp in memory, never ``os.environ``."""
    async with running_server(hass) as (_mgr, entry):
        token = entry.data[DATA_ADMIN_TOKEN]
        config_dir = os.environ["HA_MCP_CONFIG_DIR"]

        assert config_dir == hass.config.path(SERVER_CONFIG_SUBDIR)
        assert await hass.async_add_executor_job(os.path.isdir, config_dir)
        assert os.environ["HA_MCP_EMBEDDED"] == "1"
        assert "HOMEASSISTANT_URL" not in os.environ
        assert "HOMEASSISTANT_TOKEN" not in os.environ
        assert token not in os.environ.values()


async def test_the_worker_is_handed_the_connection_in_memory(
    hass: HomeAssistant,
) -> None:
    options = {OPT_SERVER_URL: f"{HA_URL}/"}  # a trailing slash is trimmed
    async with running_server(hass, options=options) as (_mgr, entry):
        config = server_config()
        settings = config.get_global_settings()

        assert settings.homeassistant_url == HA_URL
        assert settings.homeassistant_token == entry.data[DATA_ADMIN_TOKEN]
        # Home Assistant's own directory, so blueprints can be read directly (#2329).
        assert config.get_embedded_config_dir() == hass.config.config_dir
        assert config.should_emit_llm_api_metadata() is True


async def test_the_llm_api_option_reaches_the_server(hass: HomeAssistant) -> None:
    async with running_server(hass, options={OPT_ENABLE_LLM_API: False}):
        assert server_config().should_emit_llm_api_metadata() is False


@pytest.mark.parametrize(
    ("api", "options", "url", "verify_ssl"),
    [
        (
            ApiConfig("127.0.0.1", "127.0.0.1", 8443, True),
            {},
            "https://127.0.0.1:8443",
            False,
        ),
        (
            ApiConfig("127.0.0.1", "127.0.0.1", 8443, True),
            {OPT_SERVER_URL: f"{HA_URL}/"},
            HA_URL,
            True,
        ),
        (
            ApiConfig("127.0.0.1", "127.0.0.1", 8123, True),
            {OPT_SERVER_URL: "http://127.0.0.1:8123"},
            "https://127.0.0.1:8123",
            False,
        ),
    ],
    ids=["derived from ssl", "explicit override wins", "stored default is no override"],
)
async def test_the_loopback_url_follows_the_http_integrations_port_and_tls(
    hass: HomeAssistant,
    api: ApiConfig,
    options: dict[str, Any],
    url: str,
    verify_ssl: bool,
) -> None:
    """Issue #1890: plaintext into a TLS socket killed every Home Assistant round
    trip while the MCP handshake kept working; the certificate is issued for the
    hostname, never for 127.0.0.1, so verification on that hop is off."""
    hass.config.api = api
    async with running_server(hass, options=options):
        settings = server_config().get_global_settings()

        assert settings.homeassistant_url == url
        assert settings.verify_ssl is verify_ssl


# -- older servers keep working ----------------------------------------------


async def test_a_server_without_config_dir_still_gets_the_tls_setting(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    """Only the keyword the server rejects is dropped; falling back to the two
    arguments would bring back the certificate failure of #1890."""

    def old_server(config: Any) -> None:
        real = config.set_embedded_connection
        config.set_embedded_connection = lambda url, token, verify_ssl=None: real(
            url, token, verify_ssl=verify_ssl
        )

    import_hook.after("ha_mcp.config", old_server)
    hass.config.api = ApiConfig("127.0.0.1", "127.0.0.1", 8123, True)
    async with running_server(hass, options=COLD):
        config = server_config()

        assert config.get_embedded_config_dir() is None
        assert config.get_global_settings().verify_ssl is False


async def test_a_server_that_takes_only_url_and_token_still_starts(
    hass: HomeAssistant, import_hook: ImportHook, caplog: pytest.LogCaptureFixture
) -> None:
    def old_server(config: Any) -> None:
        real = config.set_embedded_connection

        def url_and_token_only(url: str, token: str) -> None:
            real(url, token)

        config.set_embedded_connection = url_and_token_only

    import_hook.after("ha_mcp.config", old_server)
    hass.config.api = ApiConfig("127.0.0.1", "127.0.0.1", 8123, True)
    async with running_server(hass, options=COLD) as (_mgr, entry):
        settings = server_config().get_global_settings()

        assert settings.homeassistant_url == "https://127.0.0.1:8123"
        assert settings.homeassistant_token == entry.data[DATA_ADMIN_TOKEN]
        assert settings.verify_ssl is True
    assert "does not accept verify_ssl" in caplog.text


async def test_the_cached_settings_are_reset_before_the_connection_is_registered(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    """A reload keeps ha-mcp imported, so settings built on the first start would
    serve stale feature flags to every later start."""
    order: list[str] = []

    def record(config: Any) -> None:
        reset, register = config.reset_global_settings, config.set_embedded_connection

        def reset_recorded() -> None:
            order.append("reset")
            reset()

        def register_recorded(*args: Any, **kwargs: Any) -> None:
            order.append("register")
            register(*args, **kwargs)

        config.reset_global_settings = reset_recorded
        config.set_embedded_connection = register_recorded

    import_hook.after("ha_mcp.config", record)
    async with running_server(hass, options=COLD):
        pass

    assert order == ["reset", "register"]


async def test_a_server_that_did_not_take_the_connection_refuses_to_serve(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    """A sentinel connection would fail every tool call while the start looked fine."""

    def ignore_connection(config: Any) -> None:
        config.set_embedded_connection = lambda *args, **kwargs: None

    import_hook.after("ha_mcp.config", ignore_connection)
    mgr, _entry = await new_manager(hass, token=True, options=server_options(**COLD))

    with pytest.raises(es.EmbeddedServerError) as err:
        await mgr.async_start()
    await mgr.async_stop()

    assert "sentinel" in str(err.value).lower()
    assert await refuses(mgr.port)


async def test_the_sdk_log_filters_are_installed(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    installed: list[bool] = []

    def record(module: Any) -> None:
        real = module.install_sdk_log_filters

        def install_recorded() -> None:
            installed.append(True)
            real()

        module.install_sdk_log_filters = install_recorded

    import_hook.after("ha_mcp.log_filters", record)
    async with running_server(hass, options=COLD):
        pass

    assert installed == [True]


@pytest.mark.parametrize(
    ("hook", "warns", "names"),
    [
        ("missing", False, ""),
        ("without helper", True, "cannot import name"),
        ("broken dependency", True, "fastmcp"),
    ],
)
async def test_the_log_filters_never_block_a_start(
    hass: HomeAssistant,
    import_hook: ImportHook,
    caplog: pytest.LogCaptureFixture,
    hook: str,
    warns: bool,
    names: str,
) -> None:
    """An older ha-mcp has no ``log_filters`` and stays silent; a dependency that
    fails to import is not that case and must be named, not swallowed."""
    caplog.set_level(logging.WARNING, logger=es.__name__)
    name = "ha_mcp.log_filters"
    if hook == "missing":
        import_hook.fail(
            name, ModuleNotFoundError(f"No module named {name!r}", name=name)
        )
    elif hook == "without helper":
        import_hook.empty(name)
    else:
        import_hook.fail(
            name, ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
        )

    async with running_server(hass, options=COLD) as (mgr, _entry):
        assert mgr.is_running

    assert ("log-noise filters" in caplog.text) is warns
    if warns:
        assert names in caplog.text


# -- a worker that fails to import --------------------------------------------


async def test_a_worker_crash_surfaces_as_a_start_error(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    import_hook.fail("ha_mcp.server", RuntimeError("serve failed"))
    mgr, _entry = await new_manager(hass, token=True, options=server_options(**COLD))

    with pytest.raises(es.EmbeddedServerError, match="worker thread crashed") as err:
        await mgr.async_start()
    await mgr.async_stop()

    assert err.value.kind == "start"
    assert "serve failed" in str(err.value)
    assert not mgr.is_running


async def test_an_import_failure_is_diagnosed_as_a_package_problem(
    hass: HomeAssistant, import_hook: ImportHook
) -> None:
    import_hook.fail(
        "ha_mcp.server",
        ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp"),
    )
    mgr, _entry = await new_manager(hass, token=True, options=server_options(**COLD))

    with pytest.raises(es.EmbeddedServerError) as err:
        await mgr.async_start()
    await mgr.async_stop()

    assert err.value.kind == "package"
    assert "fastmcp" in str(err.value)
