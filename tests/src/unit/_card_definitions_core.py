"""Home Assistant's real frontend, the card index built from it, and custom cards.

The card index reads the frontend Core serves: the ``home-assistant-frontend``
package (the dev group pins it), which ``hass_frontend.where()`` names. These
helpers give a test that real build and the real custom-card sandbox; nothing of
the frontend is written by a test.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import hass_frontend
import homeassistant
import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.ha_mcp_tools import card_definitions as cd
from custom_components.ha_mcp_tools import custom_cards as cc
from custom_components.ha_mcp_tools import dashboard_cards_ws as dcw
from ha_mcp.tools import dashboard_card_describe as describe_mod

from ._component_core import ws_result

__all__ = [
    "MINIMAL_DOM",
    "CardIndex",
    "assert_described_without_a_form",
    "card_description",
    "card_file",
    "card_name",
    "constrained_core",
    "counted_full_collections",
    "cc",
    "cd",
    "custom_cards",
    "dcw",
    "definitions",
    "describe_mod",
    "fresh_card_state",
    "frontend_strings",
    "real_definitions",
    "stalled_card_download",
]

# What the sandbox needs of linkedom to run a card file. The real DOM is
# fetched through the production cold-cache path in E2E, never vendored.
MINIMAL_DOM = """
globalThis.__linkedom = {
  HTMLElement: class HTMLElement {},
  parseHTML: () => ({document: {}, customElements: {define() {}}}),
};
"""

_I18N = "ui.panel.lovelace.editor.card."


def frontend_strings() -> dict[str, str]:
    """The English lovelace strings the installed frontend ships."""
    translations = hass_frontend.where() / "static" / "translations" / "lovelace"
    [path] = (
        p
        for p in translations.glob("en-*.json")
        if re.fullmatch(r"en-[0-9a-f]+\.json", p.name)
    )
    strings: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return strings


def card_name(card_type: str) -> str:
    """The name the installed frontend gives a built-in card type."""
    return frontend_strings()[f"{_I18N}{card_type}.name"]


def card_description(card_type: str) -> str:
    """The description the installed frontend gives a built-in card type."""
    return frontend_strings()[f"{_I18N}{card_type}.description"]


def real_definitions() -> cd.CardDefinitions:
    """The card index of the installed frontend (its engine stops with the test)."""
    return cd.CardDefinitions(hass_frontend.where())


@pytest.fixture
def definitions() -> cd.CardDefinitions:
    """The card index of the installed frontend."""
    return real_definitions()


def card_file(path: Path, tag: str, set_config: str = "") -> Path:
    """A custom card's resource file: ``tag`` is registered, ``setConfig`` runs
    ``set_config`` (JavaScript over ``c``, the config)."""
    path.write_text(
        f"class Card extends HTMLElement {{ setConfig(c) {{ {set_config} }} }}\n"
        f"customElements.define({tag!r}, Card);\n",
        encoding="utf-8",
    )
    return path


def card_file_with_editor(
    path: Path, tag: str, set_config: str = "", editor_set_config: str = ""
) -> Path:
    """A custom card's resource file whose ``getConfigElement`` returns an editor
    (``editor_set_config`` runs in its ``setConfig``); see ``card_file``."""
    path.write_text(
        "class Card extends HTMLElement {\n"
        f"  setConfig(c) {{ {set_config} }}\n"
        f"  static getConfigElement() {{ return {{ setConfig(c) {{ {editor_set_config} }} }}; }}\n"
        "}\n"
        f"customElements.define({tag!r}, Card);\n",
        encoding="utf-8",
    )
    return path


def assert_described_without_a_form(described: dict[str, Any] | None) -> None:
    """A card with no editor form lists no fields and claims no coverage."""
    assert described is not None
    assert described["fields"] is None
    assert "field_coverage" not in described
    assert "note" not in described


@pytest.fixture
def custom_cards() -> Iterator[cc.CustomCards]:
    """The custom cards of a dashboard's resources; leaving closes every sandbox."""
    custom = cc.CustomCards(MINIMAL_DOM)
    try:
        yield custom
    finally:
        custom.refresh([])


@pytest.fixture
def fresh_card_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """No card index, index build, custom-card refresh or download failure from
    another test is held, and none this test makes outlives it.

    The component has no public way to drop this state: unloading its config
    entries leaves ``card_definitions`` and ``custom_cards`` untouched, so this
    fixture is the one place a test still sets their module state.
    """
    for module, names in (
        (cd, ("_definitions", "_build_task", "_build_failed_at")),
        (cc, ("_custom", "_refresh_task", "_dom_failed_at", "_resource_error")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, None)


class FrontendLocation:
    """A spy on ``hass_frontend.where()`` that calls through to the real one.

    The index build asks ``where()`` for the frontend. ``hold`` keeps that
    question waiting until ``release``; ``unreadable`` and ``without_cards``
    make the answer a failure the installed frontend cannot be made to give.
    """

    def __init__(self, without_cards: Path) -> None:
        self._where: Callable[[], Path] = hass_frontend.where
        self._empty = without_cards
        self._gate = threading.Event()
        self._gate.set()
        self._unreadable = False
        self._empty_answer = False
        self.calls = 0

    def where(self) -> Path:
        self.calls += 1
        self._gate.wait()
        if self._unreadable:
            raise OSError("the frontend files cannot be read")
        return self._empty if self._empty_answer else self._where()

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def unreadable(self) -> None:
        self._unreadable = True

    def without_cards(self) -> None:
        self._empty_answer = True

    def readable(self) -> None:
        self._unreadable = self._empty_answer = False


class CardIndex:
    """What a test of the shared card index watches: the frontend lookups and
    the QuickJS runtime checks (a call-through spy on the real check)."""

    def __init__(self, location: FrontendLocation, runtime: AsyncMock) -> None:
        self.frontend = location
        self.runtime = runtime


@pytest.fixture
async def card_index(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_card_state: None,
) -> AsyncIterator[CardIndex]:
    """The component's card index over the real frontend, not built yet.

    The runtime check is the real one (QuickJS is installed in the dev group, so
    it answers without downloading); the spy only counts its awaits.
    """
    location = FrontendLocation(tmp_path / "no-frontend")
    monkeypatch.setattr(hass_frontend, "where", location.where)
    runtime = AsyncMock(wraps=cd.async_ensure_runtime)
    monkeypatch.setattr(cd, "async_ensure_runtime", runtime)
    try:
        yield CardIndex(location, runtime)
    finally:
        location.release()
        await hass.async_block_till_done(wait_background_tasks=True)


@contextmanager
def constrained_core(
    monkeypatch: pytest.MonkeyPatch, root: Path, constraints: str
) -> Iterator[None]:
    """Home Assistant's package constraints are ``constraints``."""
    (root / "package_constraints.txt").write_text(constraints, encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(homeassistant, "__file__", str(root / "__init__.py"))
        yield


@pytest.fixture
async def stalled_card_download(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aiohttp_client: ClientSessionGenerator,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    fresh_card_state: None,
) -> AsyncIterator[None]:
    """A dashboard resource names a custom card, and the DOM library it needs is
    still downloading: the refresh of custom cards is running and not finishing."""
    release = asyncio.Event()

    async def linkedom(request: web.Request) -> web.Response:
        await release.wait()
        raise web.HTTPServiceUnavailable

    app = web.Application()
    app.router.add_get("/linkedom.tgz", linkedom)
    server = await aiohttp_client(app)
    monkeypatch.setattr(cc, "LINKEDOM_URL", str(server.make_url("/linkedom.tgz")))
    assert await async_setup_component(hass, "lovelace", {})
    ws = await hass_ws_client(hass)
    await ws_result(
        ws, "lovelace/resources/create", res_type="module", url="/local/card.js"
    )
    try:
        yield
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)


InstallCustomCard = Callable[..., Awaitable[None]]


@pytest.fixture
async def installed_custom_card(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fresh_card_state: None,
) -> AsyncIterator[InstallCustomCard]:
    """``install(tag, set_config="")``: a custom card file under ``www`` that a
    dashboard resource names, with the DOM library already in the component's
    cache. Leaving unloads every card the component loaded."""
    assert await async_setup_component(hass, "lovelace", {})
    ws = await hass_ws_client(hass)
    www = Path(hass.config.path("www"))
    cache = Path(
        hass.config.path(
            ".storage", "ha_mcp_tools", f"linkedom-{cc.LINKEDOM_VERSION}.js"
        )
    )

    def write_dom() -> None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(MINIMAL_DOM, encoding="utf-8")

    async def install(tag: str, set_config: str = "") -> None:
        def write() -> None:
            www.mkdir(parents=True, exist_ok=True)
            card_file(www / f"{tag}.js", tag, set_config)

        await hass.async_add_executor_job(write)
        await ws_result(
            ws, "lovelace/resources/create", res_type="js", url=f"/local/{tag}.js"
        )

    await hass.async_add_executor_job(write_dom)
    try:
        yield install
    finally:
        for resource in await ws_result(ws, "lovelace/resources"):
            await ws_result(ws, "lovelace/resources/delete", resource_id=resource["id"])
        await cc.async_get_custom_cards(hass, timeout=10)
        await hass.async_block_till_done(wait_background_tasks=True)


@contextmanager
def counted_full_collections(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The quickjs contexts created meanwhile, and each call to ``gc`` on one."""
    import quickjs

    context_type = quickjs.Context
    collections: list[str] = []

    class Counted:
        def __init__(self) -> None:
            self.context = context_type()

        def __getattr__(self, name: str) -> Any:
            attribute = getattr(self.context, name)
            if name != "gc":
                return attribute

            def counting(*args: Any) -> Any:
                collections.append(name)
                return attribute(*args)

            return counting

    with monkeypatch.context() as patch:
        patch.setattr(quickjs, "Context", Counted)
        yield collections
