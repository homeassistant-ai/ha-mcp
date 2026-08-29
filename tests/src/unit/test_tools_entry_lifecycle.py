"""Unit tests for the tools-entry lifecycle and the entry-type dispatch (#2292).

Three things this pins, all of which the two-entry split (#2289/#2291/#2292)
made easy to get wrong:

1. **Registration/unload parity** — every service ``_async_setup_tools_entry``
   registers is removed by ``_async_unload_tools_entry``. Asserted as SET
   EQUALITY on the service names, so the next service added to setup without a
   matching removal fails this test *by name*, not by an opaque count. The
   extra-YAML-keys pair (#1887) was exactly that leak.
2. **hass.data cleanup** — unload drops the caller token, the extra directories
   AND the extra YAML write keys, so a still-loaded component stops granting a
   removed entry's widened access. Asserted through the live readers
   (``_caller_token_ok`` / ``_current_extra_dirs`` / ``_current_extra_yaml_keys``)
   rather than the raw dict keys, since those readers are what enforcement uses.
3. **Entry-type dispatch** — ``entry_type == "server"`` routes to the embedded
   server entry; anything else (including a *missing* ``entry_type``, the
   pre-2.1.0 shape) routes to the tools setup.

Plus the shared WebSocket surface: the tools setup registers the
``ha_mcp_tools/*`` commands, and unload deliberately does NOT unregister them
(HA exposes no counterpart to ``async_register_command``, and the server entry
may still be serving from that surface). The server entry's own half of that
contract is covered by
``test_embedded_entry.py::TestWebSocketCommandRegistration``.

Home Assistant is stubbed via ``_embedded_stubs`` (imported first so the fakes
are installed before the component module binds them); the collaborators the
tools setup reaches for at call time — the ``.storage`` ``Store``, the device
registry, the manifest read, the WS command registration — are replaced per
test, so the whole lifecycle runs with no HA install and no I/O.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
import textwrap
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools as comp  # noqa: E402

# Imported eagerly so ``async_setup_entry``'s lazy
# ``from .install_source_check import ...`` resolves from sys.modules whatever a
# peer test module has since done to the ``homeassistant.*`` stubs. An explicit
# import_module call, not an import statement: the module is wanted purely for
# its sys.modules side effect, and the call form says so (a bare import here
# reads as unused to linters that ignore noqa, e.g. CodeQL).
importlib.import_module("custom_components.ha_mcp_tools.install_source_check")
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    TOOLS_ENTRY_TITLE,
)

_EMBEDDED_ENTRY_MODULE = "custom_components.ha_mcp_tools.embedded_entry"


def _make_hass(tmp_path) -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.config.config_dir = str(tmp_path)
    # The only executor offload the tools setup makes is the legacy-backup
    # migration; (0, 0) means "nothing to migrate", so the notification branch
    # stays out of the lifecycle under test.
    hass.async_add_executor_job = AsyncMock(return_value=(0, 0))
    hass.services.async_register = MagicMock(name="async_register")
    hass.services.async_remove = MagicMock(name="async_remove")
    return hass


def _make_entry(entry_type: str | None = None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.entry_id = "tools-entry-1"
    # Not the pre-#1853 default, so the retitle branch stays out of the way.
    entry.title = TOOLS_ENTRY_TITLE
    entry.data = {} if entry_type is None else {CONF_ENTRY_TYPE: entry_type}
    return entry


def _make_call(token: str | None) -> MagicMock:
    call = MagicMock(name="ServiceCall")
    call.data = {} if token is None else {comp.CALLER_TOKEN_FIELD: token}
    return call


def _service_names(mock: MagicMock) -> set[str]:
    """Service names the mock was called with as ``(DOMAIN, <service>, ...)``."""
    return {
        call.args[1]
        for call in mock.call_args_list
        if len(call.args) >= 2 and call.args[0] == DOMAIN
    }


def _called_names(func: Any) -> set[str]:
    """Every callee expression in ``func``, as source text.

    AST-based rather than a substring scan of the source: comments and
    docstrings routinely mention names the code must NOT call.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }


@pytest.fixture
def store_backing() -> dict[str, Any]:
    """In-memory stand-in for the component's three ``.storage`` blobs."""
    return {}


@pytest.fixture
def tools_env(monkeypatch, store_backing):
    """Replace the collaborators the tools setup reaches for at call time.

    ``Store`` (three separate stores: token, allowed paths, extra YAML keys),
    the device registry, the manifest version read and the WebSocket command
    registration are all module-level names in the component, so patching them
    here keeps ``_async_setup_tools_entry`` runnable without Home Assistant.
    """

    class _FakeStore:
        def __init__(self, hass: Any, version: int, key: str) -> None:
            self._key = key

        async def async_load(self) -> Any:
            return store_backing.get(self._key)

        async def async_save(self, data: Any) -> None:
            store_backing[self._key] = data

    register_commands = MagicMock(name="async_register_commands")
    monkeypatch.setattr(comp, "Store", _FakeStore)
    monkeypatch.setattr(comp, "async_register_commands", register_commands)
    monkeypatch.setattr(comp, "dr", MagicMock(name="device_registry"))
    monkeypatch.setattr(
        comp,
        "async_get_integration",
        AsyncMock(return_value=SimpleNamespace(version="2.1.0")),
    )
    return SimpleNamespace(
        register_commands=register_commands,
        storage=store_backing,
    )


class TestServiceRegistrationParity:
    """Setup and unload must cover exactly the same set of services.

    The load-bearing test of this module: ``_async_unload_tools_entry`` is a
    hand-maintained list, so a service added to setup alone survives the unload
    and keeps answering after the entry is gone.
    """

    async def test_unload_removes_every_service_setup_registers(
        self, tools_env, tmp_path
    ):
        hass = _make_hass(tmp_path)
        entry = _make_entry()

        assert await comp._async_setup_tools_entry(hass, entry) is True
        registered = _service_names(hass.services.async_register)
        assert registered, "the tools setup registered no services at all"

        assert await comp._async_unload_tools_entry(hass, entry) is True
        removed = _service_names(hass.services.async_remove)

        assert registered == removed, (
            "every service the tools setup registers must be removed on "
            "unload; registered but never removed: "
            f"{sorted(registered - removed)}; removed but never registered: "
            f"{sorted(removed - registered)}"
        )

    async def test_extra_yaml_keys_services_are_on_both_sides(
        self, tools_env, tmp_path
    ):
        """The #2292 pair specifically — set equality alone would also be
        satisfied by registering neither."""
        hass = _make_hass(tmp_path)
        entry = _make_entry()

        await comp._async_setup_tools_entry(hass, entry)
        await comp._async_unload_tools_entry(hass, entry)

        pair = {comp.SERVICE_GET_EXTRA_YAML_KEYS, comp.SERVICE_SET_EXTRA_YAML_KEYS}
        assert pair <= _service_names(hass.services.async_register)
        assert pair <= _service_names(hass.services.async_remove)


class TestHassDataCleanup:
    """Unload drops the token, the extra dirs and the extra YAML keys.

    Asserted through the live readers rather than the raw ``hass.data`` keys:
    those readers are what the handlers and the enforcement path consult, so a
    value left behind is a permission the unloaded entry still grants.
    """

    async def test_unload_revokes_the_cached_caller_token(
        self, tools_env, tmp_path, store_backing
    ):
        store_backing[comp._TOKEN_STORAGE_KEY] = {"token": "tok-abc"}
        hass = _make_hass(tmp_path)
        entry = _make_entry()

        await comp._async_setup_tools_entry(hass, entry)
        assert comp._caller_token_ok(hass, _make_call("tok-abc")) is True

        await comp._async_unload_tools_entry(hass, entry)

        assert comp._caller_token_ok(hass, _make_call("tok-abc")) is False

    async def test_unload_reverts_the_extra_directories(
        self, tools_env, tmp_path, store_backing
    ):
        store_backing[comp._ALLOWED_PATHS_STORAGE_KEY] = {"paths": ["my_scripts"]}
        hass = _make_hass(tmp_path)
        entry = _make_entry()

        await comp._async_setup_tools_entry(hass, entry)
        assert comp._current_extra_dirs(hass) == ["my_scripts"]

        await comp._async_unload_tools_entry(hass, entry)

        assert comp._current_extra_dirs(hass) == []

    async def test_unload_reverts_the_extra_yaml_write_keys(
        self, tools_env, tmp_path, store_backing
    ):
        """The #2292 fix: a leftover list would keep widening the YAML write
        allowlist after the entry that configured it is gone."""
        store_backing[comp._EXTRA_YAML_KEYS_STORAGE_KEY] = {"keys": ["rest_command"]}
        hass = _make_hass(tmp_path)
        entry = _make_entry()

        await comp._async_setup_tools_entry(hass, entry)
        assert comp._current_extra_yaml_keys(hass) == ["rest_command"]

        await comp._async_unload_tools_entry(hass, entry)

        assert comp._current_extra_yaml_keys(hass) == []


class TestEntryTypeDispatch:
    """``async_setup_entry`` / ``async_unload_entry`` route on ``entry_type``.

    A missing ``entry_type`` means the tools entry: pre-2.1.0 entries carry no
    such field and must keep working across the component update with no
    migration.
    """

    @pytest.fixture
    def routes(self, monkeypatch):
        """Fake both destinations so only the routing decision is exercised.

        The server side is faked as a whole ``sys.modules`` entry because the
        entry points import it lazily, at call time — the real module would drag
        in the entire embedded chain for a test that only cares about which of
        the two functions was reached.
        """
        server_setup = AsyncMock(name="async_setup_server_entry", return_value=True)
        server_unload = AsyncMock(name="async_unload_server_entry", return_value=True)
        fake_embedded = ModuleType(_EMBEDDED_ENTRY_MODULE)
        fake_embedded.async_setup_server_entry = server_setup
        fake_embedded.async_unload_server_entry = server_unload
        monkeypatch.setitem(sys.modules, _EMBEDDED_ENTRY_MODULE, fake_embedded)

        tools_setup = AsyncMock(name="_async_setup_tools_entry", return_value=True)
        tools_unload = AsyncMock(name="_async_unload_tools_entry", return_value=True)
        monkeypatch.setattr(comp, "_async_setup_tools_entry", tools_setup)
        monkeypatch.setattr(comp, "_async_unload_tools_entry", tools_unload)
        return SimpleNamespace(
            server_setup=server_setup,
            server_unload=server_unload,
            tools_setup=tools_setup,
            tools_unload=tools_unload,
        )

    async def test_server_entry_type_sets_up_the_server_entry(self, routes, tmp_path):
        hass = _make_hass(tmp_path)
        entry = _make_entry(ENTRY_TYPE_SERVER)

        assert await comp.async_setup_entry(hass, entry) is True

        routes.server_setup.assert_awaited_once_with(hass, entry)
        routes.tools_setup.assert_not_awaited()

    @pytest.mark.parametrize(
        "entry_type",
        [None, ENTRY_TYPE_TOOLS, "future_entry_type"],
        ids=["missing", "explicit_tools", "other_nonserver"],
    )
    async def test_non_server_entry_type_sets_up_the_tools_entry(
        self, routes, tmp_path, entry_type
    ):
        hass = _make_hass(tmp_path)
        entry = _make_entry(entry_type)

        assert await comp.async_setup_entry(hass, entry) is True

        routes.tools_setup.assert_awaited_once_with(hass, entry)
        routes.server_setup.assert_not_awaited()

    async def test_server_entry_type_unloads_the_server_entry(self, routes, tmp_path):
        hass = _make_hass(tmp_path)
        entry = _make_entry(ENTRY_TYPE_SERVER)

        assert await comp.async_unload_entry(hass, entry) is True

        routes.server_unload.assert_awaited_once_with(hass, entry)
        routes.tools_unload.assert_not_awaited()

    @pytest.mark.parametrize(
        "entry_type",
        [None, ENTRY_TYPE_TOOLS, "future_entry_type"],
        ids=["missing", "explicit_tools", "other_nonserver"],
    )
    async def test_non_server_entry_type_unloads_the_tools_entry(
        self, routes, tmp_path, entry_type
    ):
        hass = _make_hass(tmp_path)
        entry = _make_entry(entry_type)

        assert await comp.async_unload_entry(hass, entry) is True

        routes.tools_unload.assert_awaited_once_with(hass, entry)
        routes.server_unload.assert_not_awaited()


class TestWebSocketCommandSurface:
    """Both entry setups register the shared ``ha_mcp_tools/*`` commands, and
    unload leaves them alone.

    The server-entry half is pinned by
    ``test_embedded_entry.py::TestWebSocketCommandRegistration``; this covers
    the tools half and the unload side. HA offers no counterpart to
    ``async_register_command``, and the surface is shared with the server entry,
    so unloading the tools entry must not attempt to tear it down (#2292).
    """

    async def test_tools_setup_registers_the_ws_commands(self, tools_env, tmp_path):
        hass = _make_hass(tmp_path)

        await comp._async_setup_tools_entry(hass, _make_entry())

        tools_env.register_commands.assert_called_once_with(hass)

    async def test_unload_does_not_touch_the_ws_commands(self, tools_env, tmp_path):
        hass = _make_hass(tmp_path)
        entry = _make_entry()
        await comp._async_setup_tools_entry(hass, entry)
        tools_env.register_commands.reset_mock()

        await comp._async_unload_tools_entry(hass, entry)

        tools_env.register_commands.assert_not_called()

    def test_unload_calls_nothing_that_registers_or_unregisters_commands(self):
        """Source-level companion to the mock above: an unregister attempt would
        be a NEW call to something this test cannot pre-stub, so no mock can
        observe it. Reads the calls out of the AST, so the design note in the
        function body — which does name the WS surface — is not a false hit."""
        called = _called_names(comp._async_unload_tools_entry)
        offenders = sorted(
            name
            for name in called
            if "register" in name.lower() or "websocket" in name.lower()
        )
        assert not offenders, (
            "the shared ha_mcp_tools/* WS command surface must survive a "
            f"tools-entry unload, but it calls: {offenders}"
        )
