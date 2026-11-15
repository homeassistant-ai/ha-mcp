"""A built Home Assistant frontend on disk, and the card index read from it.

The card index reads the frontend files ``hass_frontend`` serves; the package
is not installed with Core's test kit, so these helpers write a small build
with the same shape (webpack chunks, a registered editor per card, lovelace
translations) and a ``hass_frontend`` package that names it. The index, the
``dashboard_cards`` command and the describe tool then run for real on it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
    "FrontendOnDisk",
    "build_definitions",
    "card_file",
    "counted_full_collections",
    "cc",
    "cd",
    "custom_cards",
    "dcw",
    "describe_mod",
    "frontend",
    "idle_custom_cards",
    "stalled_card_download",
    "write_frontend",
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
_STRINGS = {
    f"{_I18N}tile.name": "Tile",
    f"{_I18N}tile.description": "An entity at a glance.",
    f"{_I18N}entities.name": "Entities",
}

# Module 6: a small struct library. ``object`` validates a config the way the
# frontend's superstruct does: an unlisted key fails as ``never``, a value
# outside ``enums`` fails as ``enums``, a nested object is checked in turn.
_STRUCT_LIB = (
    "6(m,e,r){"
    "function check(schema,c,path,out){Object.keys(c).forEach((k)=>{"
    'const at=path.concat(k),child=schema[k],where=at.join(".");'
    'if(!child){out.push({path:at,type:"never",message:"At path: "+where+'
    '" -- Expected a value of type `never`, but received: `"+'
    'JSON.stringify(c[k])+"`"});return}'
    "if(child.values&&child.values.indexOf(c[k])<0){out.push({path:at,"
    'type:"enums",message:"At path: "+where+" -- Expected one of `"+'
    'child.values.map((v)=>JSON.stringify(v)).join("|")+"`, but received: `"+'
    'JSON.stringify(c[k])+"`"});return}'
    'if(child.schema&&c[k]&&typeof c[k]==="object")check(child.schema,c[k],at,out)'
    "})}"
    "r.d(e,{},{"
    'string:{type:"string"},'
    'enums:(values)=>({type:"enums",values}),'
    "memo:(fn)=>fn,"
    "assert:(c,s)=>{},"
    'object:(schema)=>({type:"object",schema,validate:(c)=>{'
    "const out=[];check(schema,c,[],out);"
    "return out.length?[{failures:()=>out}]:[undefined,c]}})"
    "})}"
)

# A struct-checked editor: ``setConfig`` asserts the config against ``v``.
TILE_EDITOR = (
    "var s=r(6),v=(0,s.object)({type:s.string,entity:s.string,color:s.string,"
    'tap_action:(0,s.object)({action:(0,s.enums)(["none"])})});'
    "class C{setConfig(c){(0,s.assert)(c,v)}}"
)
ENTITIES_EDITOR = (
    "var s=r(6),v=(0,s.object)({type:s.string,title:s.string});"
    "class C{setConfig(c){(0,s.assert)(c,v)}}"
)


def write_frontend(
    root: Path,
    editors: Mapping[str, str] | None = None,
    *,
    cards: Sequence[str] = (),
    modules: str = "",
    files: Mapping[str, str] | None = None,
) -> None:
    """Write a frontend build under ``root`` (``frontend_latest`` and the
    translations): the ``tile`` and ``entities`` cards, plus ``cards``.

    ``editors`` maps a card type to the body of its editor module (it replaces
    the default editor of ``tile`` and ``entities``); ``modules`` is more
    chunk text (``100(m,e,r){...}``); ``files`` are further built files.
    """
    editor_bodies = {
        "tile": TILE_EDITOR,
        "entities": ENTITIES_EDITOR,
        **(editors or {}),
    }
    listed: list[str] = [
        f'{8 + i}(e){{(0,e.EM)("hui-{card}-card");class A{{setConfig(c){{}}}}}}'
        for i, card in enumerate(("tile", "entities", *cards))
    ]
    listed.append(_STRUCT_LIB)
    listed.extend(
        f'{20 + i}(m,e,r){{(0,e.EM)("hui-{card}-card-editor");{body}}}'
        for i, (card, body) in enumerate(editor_bodies.items())
    )
    if modules:
        listed.append(modules)
    frontend_dir = root / "frontend_latest"
    frontend_dir.mkdir(parents=True, exist_ok=True)
    (frontend_dir / "cards.js").write_text(
        "export const __webpack_modules__={" + ",".join(listed) + "};",
        encoding="utf-8",
    )
    for name, text in (files or {}).items():
        (frontend_dir / name).write_text(text, encoding="utf-8")
    translations = root / "static" / "translations" / "lovelace"
    translations.mkdir(parents=True, exist_ok=True)
    (translations / "en-abc123.json").write_text(json.dumps(_STRINGS), encoding="utf-8")


def build_definitions(root: Path, *args: Any, **kwargs: Any) -> cd.CardDefinitions:
    """The card index of a frontend written under ``root`` (see ``write_frontend``)."""
    write_frontend(root, *args, **kwargs)
    return cd.CardDefinitions(root)


def card_file(path: Path, tag: str, set_config: str = "") -> Path:
    """A custom card's resource file: ``tag`` is registered, ``setConfig`` runs
    ``set_config`` (JavaScript over ``c``, the config)."""
    path.write_text(
        f"class Card extends HTMLElement {{ setConfig(c) {{ {set_config} }} }}\n"
        f"customElements.define({tag!r}, Card);\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def custom_cards() -> Iterator[cc.CustomCards]:
    """The custom cards of a dashboard's resources; leaving closes every sandbox."""
    custom = cc.CustomCards(MINIMAL_DOM)
    try:
        yield custom
    finally:
        custom.refresh([])


@pytest.fixture
def idle_custom_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    """No refresh of dashboard resources is running or has failed."""
    for name in ("_custom", "_refresh_task", "_dom_failed_at", "_resource_error"):
        monkeypatch.setattr(cc, name, None)


_PACKAGE = '''\
"""Names a built frontend, as the hass_frontend package does."""

import threading
from pathlib import Path

ROOT = Path({root!r})
RELEASE = threading.Event()
RELEASE.set()
BROKEN = False
CALLS = []


def where():
    CALLS.append(1)
    RELEASE.wait()
    if BROKEN:
        raise RuntimeError("the frontend files cannot be read")
    return ROOT
'''


class FrontendOnDisk:
    """The frontend ``hass_frontend`` names, and the build that reads it."""

    def __init__(self, root: Path, package: Any, runtime: AsyncMock) -> None:
        self.root = root
        self.runtime = runtime
        self._package = package

    def write(self, *args: Any, **kwargs: Any) -> None:
        """Replace the build before the index is first read (see ``write_frontend``)."""
        write_frontend(self.root, *args, **kwargs)

    def hold(self) -> None:
        """The build stays in ``hass_frontend.where()`` until ``release``."""
        self._package.RELEASE.clear()

    def release(self) -> None:
        self._package.RELEASE.set()

    def break_files(self) -> None:
        """The frontend cannot be read: the index build fails."""
        self._package.BROKEN = True

    @property
    def builds(self) -> int:
        """How many times the index build asked where the frontend is."""
        return len(self._package.CALLS)


@pytest.fixture
async def frontend(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FrontendOnDisk]:
    """A frontend the component's card index reads, on a real ``hass``.

    The index starts unbuilt. The quickjs runtime check (which looks for the
    version the component installs on a real board) answers that it is ready.
    """
    root = tmp_path / "frontend"
    write_frontend(root)
    site = tmp_path / "site"
    package = site / "hass_frontend"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        _PACKAGE.format(root=str(root)), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(site))
    sys.modules.pop("hass_frontend", None)
    runtime = AsyncMock(return_value=True)
    monkeypatch.setattr(cd, "async_ensure_runtime", runtime)
    for name in ("_definitions", "_build_task", "_build_failed_at"):
        monkeypatch.setattr(cd, name, None)
    served = FrontendOnDisk(root, importlib.import_module("hass_frontend"), runtime)
    try:
        yield served
    finally:
        served.release()
        await hass.async_block_till_done(wait_background_tasks=True)
        sys.modules.pop("hass_frontend", None)


@pytest.fixture
async def stalled_card_download(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aiohttp_client: ClientSessionGenerator,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    idle_custom_cards: None,
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


def add_card_names(root: Path, names: Mapping[str, str]) -> None:
    """Give cards a translated name in a frontend written under ``root``."""
    translations = root / "static" / "translations" / "lovelace" / "en-abc123.json"
    strings = json.loads(translations.read_text(encoding="utf-8"))
    strings.update({f"{_I18N}{card}.name": name for card, name in names.items()})
    translations.write_text(json.dumps(strings), encoding="utf-8")


InstallCustomCard = Callable[..., Awaitable[None]]


@pytest.fixture
async def installed_custom_card(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    idle_custom_cards: None,
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
