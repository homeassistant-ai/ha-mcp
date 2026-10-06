"""Custom (``custom:``) cards, asked about themselves (issue #2632).

A custom card ships as a JavaScript file registered as a dashboard resource
(``/hacsfiles/...`` from HACS, ``/local/...`` from ``www``). Each file runs in
its own QuickJS sandbox on top of linkedom, a DOM written for non-browser
runtimes: no network, no filesystem. The card then answers through the same
calls the dashboard makes: its editor's and its own ``setConfig`` reject a
config they cannot show, and its editor's form lists its fields. A sandbox
crash (a browser API the DOM lacks) is never reported as a card problem.

linkedom is an npm package, so it is fetched once from the npm registry at a
pinned version, checked against the registry's integrity hash, and cached in
``.storage``. Without it (offline), custom cards are simply not checked.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

LINKEDOM_VERSION = "0.18.13"
LINKEDOM_INTEGRITY = "sha512-ES/o9qotMpzpN2MHs+Iq/JcVoOj8Fa5wiQYrTdFpvAnwXL0g66XHHUc9WUMk6nAlBtGsFQ24ne+SYnvnaQ2FSw=="
LINKEDOM_URL = f"https://registry.npmjs.org/linkedom/-/linkedom-{LINKEDOM_VERSION}.tgz"
_RETRY_AFTER_S = 600.0
_RESOURCE_PREFIXES = (("/hacsfiles/", "www/community"), ("/local/", "www"))
# Errors a card raises on purpose are Error or StructError; these come from a
# browser API the sandbox lacks, so they say nothing about the config.
_SANDBOX_ERRORS = ("TypeError", "ReferenceError", "RangeError", "SyntaxError")

_RUNTIME_JS = r"""
var __pending = {};
function __bootDom() {
  var dom = __linkedom.parseHTML('<!doctype html><html><head></head><body></body></html>');
  var g = globalThis;
  // Every DOM class linkedom exports, so `instanceof` checks have a target;
  // the window's own (document-bound) class wins where it has one.
  Object.keys(__linkedom).forEach(function (n) {
    if (/^[A-Z]/.test(n)) g[n] = dom[n] !== undefined ? dom[n] : __linkedom[n];
  });
  g.document = dom.document;
  g.window = g; g.self = g; g.parent = g; g.top = g;
  var registry = g.__registry = {};
  var define = dom.customElements.define.bind(dom.customElements);
  g.customElements = {
    define: function (n, c, o) { registry[n] = c; try { define(n, c, o); } catch (e) {} },
    get: function (n) { return registry[n]; },
    whenDefined: function () { return Promise.resolve(); },
    upgrade: function () {},
  };
  var noop = function () {};
  g.__timers = [];
  g.setTimeout = function (f) { if (typeof f === 'function') g.__timers.push(f); return g.__timers.length; };
  g.requestAnimationFrame = g.setTimeout;
  g.clearTimeout = g.clearInterval = g.cancelAnimationFrame = noop;
  g.setInterval = function () { return 0; };
  g.queueMicrotask = function (f) { Promise.resolve().then(f); };
  g.addEventListener = g.removeEventListener = noop;
  g.dispatchEvent = function () { return true; };
  var observer = function () { return { observe: noop, unobserve: noop, disconnect: noop }; };
  g.ResizeObserver = g.IntersectionObserver = observer;
  if (!g.MutationObserver) g.MutationObserver = observer;
  if (!g.CSSStyleSheet) { g.CSSStyleSheet = function () {}; g.CSSStyleSheet.prototype.replaceSync = noop;
    g.CSSStyleSheet.prototype.replace = function () { return Promise.resolve(this); }; }
  g.navigator = { userAgent: 'quickjs', language: 'en', languages: ['en'], maxTouchPoints: 0 };
  g.location = { href: 'http://localhost/', pathname: '/', search: '', hash: '', origin: 'http://localhost' };
  g.history = { pushState: noop, replaceState: noop };
  g.localStorage = g.sessionStorage = { getItem: function () { return null; }, setItem: noop, removeItem: noop };
  g.matchMedia = function () { return { matches: false, addListener: noop, removeListener: noop,
    addEventListener: noop, removeEventListener: noop }; };
  g.getComputedStyle = function () { return { getPropertyValue: function () { return ''; } }; };
  g.fetch = function () { return Promise.reject(new Error('no network')); };
  g.screen = { width: 1280, height: 800 }; g.devicePixelRatio = 1; g.innerWidth = 1280; g.innerHeight = 800;
  g.performance = { now: function () { return Date.now(); }, mark: noop, measure: noop };
  g.CSS = { supports: function () { return false; }, escape: function (s) { return s; } };
  g.getSelection = function () { return null; }; g.scrollTo = noop;
  if (!g.console) g.console = { log: noop, info: noop, warn: noop, error: noop, debug: noop };
}
var __hass = { localize: function (k) { return k; }, states: {}, entities: {}, devices: {}, areas: {},
  config: { components: [], version: '', unit_system: {} }, locale: { language: 'en' }, language: 'en',
  themes: { darkMode: false, themes: {} }, user: { is_admin: true }, services: {},
  connection: { subscribeMessage: function () { return Promise.resolve(function () {}); } },
  callWS: function () { return Promise.resolve({}); }, formatEntityState: function () { return ''; } };
function __verdict(e) {
  var name = e && e.name;
  return { sandbox: __SANDBOX.indexOf(name) >= 0, message: String((e && e.message) || e) };
}
function __plain(v) {
  return JSON.parse(JSON.stringify(v, function (k, x) { return typeof x === 'function' ? undefined : x; }));
}
function __formOf(value) {
  var found = null;
  (function walk(v, depth) {
    if (found || !v || depth > 8) return;
    if (Array.isArray(v)) {
      if (v.length && v.every(function (x) { return x && typeof x === 'object' && 'name' in x &&
          ('selector' in x || 'schema' in x || 'type' in x); })) { found = v; return; }
      v.forEach(function (x) { walk(x, depth + 1); });
    } else if (typeof v === 'object' && v.values) { walk(v.values, depth + 1); }
  })(value, 0);
  return found ? __plain(found) : null;
}
function card(op, p) {
  try {
    if (op === 'eval') { (0, eval)(p); return {}; }
    if (op === 'boot') { __bootDom(); return {}; }
    if (op === 'pump') { var fs = __timers; __timers = []; fs.forEach(function (f) { try { f(); } catch (e) {} });
      return { value: fs.length }; }
    if (op === 'cards') { return { value: { tags: Object.keys(__registry), cards: (window.customCards || [])
      .map(function (c) { return { type: c.type, name: c.name, description: c.description }; }) } }; }
    var C = __registry[p.tag];
    if (!C) return { error: 'not registered' };
    if (op === 'prepare') {
      var slot = __pending[p.tag] = {};
      if (C.getConfigForm) Promise.resolve(C.getConfigForm()).then(function (f) { slot.form = f; }, function () {});
      if (C.getConfigElement) Promise.resolve(C.getConfigElement()).then(function (e) { slot.editor = e; }, function () {});
      return {};
    }
    var slot2 = __pending[p.tag] || {};
    if (op === 'check') {
      var problems = [];
      var targets = [];
      if (slot2.editor && slot2.editor.setConfig) targets.push(slot2.editor);
      try { var el = new C(); el.hass = __hass; targets.push(el); } catch (e) {}
      targets.forEach(function (t) {
        try { t.hass = __hass; t.setConfig(p.config); }
        catch (e) { var v = __verdict(e); if (!v.sandbox) problems.push(v.message); }
      });
      return { value: problems.filter(function (m, i) { return problems.indexOf(m) === i; }) };
    }
    if (op === 'form') {
      if (slot2.form && slot2.form.schema) return { value: __plain(slot2.form.schema) };
      var ed = slot2.editor;
      if (!ed || !ed.render) return { value: null };
      ed.hass = __hass;
      try { ed.setConfig(p.config); } catch (e) {}
      return { value: __formOf(ed.render()) };
    }
    return { error: 'unknown op' };
  } catch (e) {
    return { error: String(e) };
  }
}
"""


def resource_path(config_dir: Path, url: str) -> Path | None:
    """The file a dashboard resource URL serves, kept inside its www folder."""
    path = unquote(url.split("?", 1)[0].split("#", 1)[0])
    for prefix, folder in _RESOURCE_PREFIXES:
        if path.startswith(prefix):
            base = (config_dir / folder).resolve()
            target = (base / path[len(prefix) :]).resolve()
            if target.is_relative_to(base) and target.suffix == ".js":
                return target
    return None


def dom_script(tarball: bytes) -> str:
    """linkedom's single-file build, verified and made a plain script."""
    digest = base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    if f"sha512-{digest}" != LINKEDOM_INTEGRITY:
        raise ValueError("linkedom tarball does not match its pinned integrity hash")
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        member = archive.extractfile("package/worker.js")
        if member is None:
            raise ValueError("linkedom tarball has no worker.js")
        source = member.read().decode("utf-8")
    source = re.sub(r"^export const ", "const ", source, flags=re.MULTILINE)
    block = re.search(r"^export \{([^}]*)\};?\s*$", source, flags=re.MULTILINE)
    if block is None:
        raise ValueError("linkedom worker.js changed shape")
    names = []
    for item in block.group(1).split(","):
        local, _, exported = item.strip().partition(" as ")
        names.append(f"{exported or local}: {local}")
    exports = "globalThis.__linkedom = {" + ", ".join(names) + "};"
    # A function scope keeps linkedom's internals (its own CSSStyleSheet, ...)
    # from becoming globals a card would mistake for the browser's.
    return "(function () {\n" + source[: block.start()] + exports + "\n})();"


class _Bundle:
    """One resource file running in its own sandbox."""

    def __init__(self, dom: str, source: str) -> None:
        import quickjs

        self.engine = quickjs.Function(
            "card", _RUNTIME_JS.replace("__SANDBOX", repr(list(_SANDBOX_ERRORS)))
        )
        self.engine.set_memory_limit(256 * 1024 * 1024)
        self.engine.set_time_limit(10)
        for step, payload in (("eval", dom), ("boot", None), ("eval", source)):
            error = self.engine(step, payload).get("error")
            if error:
                raise ValueError(error)
        self._settle()
        listed = self.engine("cards", None)["value"]
        self.tags: list[str] = listed["tags"]
        self.cards: list[dict[str, Any]] = listed["cards"]
        for tag in self.tags:
            self.engine("prepare", {"tag": tag})
        self._settle()

    def _settle(self) -> None:
        """Run the bundle's queued promises and timers (lazy editors load here)."""
        for _ in range(50):
            while self.engine.execute_pending_job():
                pass
            if not self.engine("pump", None).get("value"):
                return

    def check(self, tag: str, config: dict[str, Any]) -> list[str]:
        return list(
            self.engine("check", {"tag": tag, "config": config}).get("value") or []
        )

    def form(self, tag: str) -> list[Any] | None:
        value = self.engine("form", {"tag": tag, "config": {"type": f"custom:{tag}"}})
        return value.get("value")


class CustomCards:
    """The custom cards the dashboard resources register, by element tag."""

    def __init__(self, dom: str) -> None:
        self._dom = dom
        self._bundles: dict[Path, tuple[float, _Bundle | None]] = {}

    def refresh(self, files: list[Path]) -> None:
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if path in self._bundles and self._bundles[path][0] == mtime:
                continue
            try:
                bundle: _Bundle | None = _Bundle(
                    self._dom, path.read_text(encoding="utf-8")
                )
            except Exception:
                _LOGGER.debug("Custom card bundle %s did not load", path, exc_info=True)
                bundle = None
            self._bundles[path] = (mtime, bundle)

    def _owner(self, tag: str) -> _Bundle | None:
        for _, bundle in self._bundles.values():
            if bundle is not None and tag in bundle.tags:
                return bundle
        return None

    def check(self, tag: str, config: dict[str, Any]) -> list[str] | None:
        """Problems the card reports, or ``None`` when no loaded bundle defines it."""
        bundle = self._owner(tag)
        return None if bundle is None else bundle.check(tag, config)

    def card_types(self) -> list[dict[str, Any]]:
        return [
            {**card, "type": f"custom:{card['type']}"}
            for _, bundle in self._bundles.values()
            if bundle is not None
            for card in bundle.cards
            if isinstance(card.get("type"), str)
        ]

    def describe(self, tag: str) -> dict[str, Any] | None:
        bundle = self._owner(tag)
        if bundle is None:
            return None
        listed = next((c for c in bundle.cards if c.get("type") == tag), {})
        return {
            "type": f"custom:{tag}",
            "name": listed.get("name"),
            "description": listed.get("description"),
            "fields": bundle.form(tag),
        }


_dom_failed_at: float | None = None
_custom: CustomCards | None = None


async def _async_dom(hass: HomeAssistant) -> str | None:
    global _dom_failed_at
    if (
        _dom_failed_at is not None
        and time.monotonic() - _dom_failed_at < _RETRY_AFTER_S
    ):
        return None
    cache = Path(
        hass.config.path(".storage", "ha_mcp_tools", f"linkedom-{LINKEDOM_VERSION}.js")
    )
    try:
        if await hass.async_add_executor_job(cache.is_file):
            dom: str = await hass.async_add_executor_job(cache.read_text, "utf-8")
            return dom
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        async with async_get_clientsession(hass).get(LINKEDOM_URL, timeout=30) as resp:
            resp.raise_for_status()
            tarball = await resp.read()
        dom = await hass.async_add_executor_job(dom_script, tarball)
        await hass.async_add_executor_job(_write_cache, cache, dom)
    except Exception:
        _LOGGER.warning(
            "Custom card checks are unavailable: linkedom could not be loaded",
            exc_info=True,
        )
        _dom_failed_at = time.monotonic()
        return None
    return dom


def _write_cache(cache: Path, dom: str) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    partial = cache.with_suffix(".tmp")
    partial.write_text(dom, encoding="utf-8")
    partial.replace(cache)


async def _async_resource_files(hass: HomeAssistant) -> list[Path]:
    from .websocket_api.dashboards import _lovelace_container

    resources = getattr(_lovelace_container(hass), "resources", None)
    if resources is None:
        return []
    await resources.async_get_info()  # loads a storage collection on first use
    items = resources.async_items()
    config_dir = Path(hass.config.config_dir)
    files = []
    for item in items or []:
        if item.get("type") in ("module", "js") and isinstance(item.get("url"), str):
            if (path := resource_path(config_dir, item["url"])) is not None:
                files.append(path)
    return files


async def async_get_custom_cards(hass: HomeAssistant) -> CustomCards | None:
    """The custom cards of the current dashboard resources; ``None`` without linkedom."""
    global _custom
    try:
        files = await _async_resource_files(hass)
        if not files:
            return _custom
        if _custom is None:
            dom = await _async_dom(hass)
            if dom is None:
                return None
            _custom = CustomCards(dom)
        await hass.async_add_executor_job(_custom.refresh, files)
    except Exception:
        _LOGGER.debug("Custom cards are unavailable", exc_info=True)
    return _custom
