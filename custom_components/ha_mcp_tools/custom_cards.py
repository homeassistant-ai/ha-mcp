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

import asyncio
import base64
import hashlib
import io
import logging
import re
import tarfile
import threading
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
# A card file is the user's own installed frontend code, but it still runs in
# Home Assistant's process: each sandbox and the whole set are capped, and a
# card that spins (endless promises or timers) is cut off rather than awaited.
_BUNDLE_MEMORY = 32 * 1024 * 1024
_TOTAL_MEMORY = 128 * 1024 * 1024
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_DOM_DOWNLOAD_BYTES = 2 * 1024 * 1024
_CALL_SECONDS = 5
# Editor enrichment must leave time for later cards in a dashboard save.
_EDITOR_SECONDS = 0.5
_PREPARE_SECONDS = 15.0
_REFRESH_SECONDS = 10.0
_SETTLE_JOBS = 20_000
_SETTLE_SECONDS = 5.0
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
      return { value: !!(C.getConfigForm || C.getConfigElement) };
    }
    var slot2 = __pending[p.tag] || {};
    if (op === 'check') {
      var problems = [];
      var targets = [];
      if (p.source !== 'editor') {
        try { var el = new C(); el.hass = __hass; targets.push({source: 'card', target: el}); } catch (e) {}
      }
      if (p.source !== 'card' && slot2.editor && slot2.editor.setConfig) targets.push({source: 'editor', target: slot2.editor});
      targets.forEach(function (t) {
        try { t.target.hass = __hass; t.target.setConfig(p.config); }
        catch (e) { var v = __verdict(e);
          if (!v.sandbox && !problems.some(function (p) { return p.message === v.message; }))
            problems.push({source: t.source, message: v.message}); }
      });
      return { value: problems };
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

    def __init__(
        self,
        dom: str,
        source: str,
        *,
        module: bool = False,
        deadline: float | None = None,
    ) -> None:
        import quickjs

        self.engine = quickjs.Function(
            "card",
            _RUNTIME_JS.replace("__SANDBOX", repr(list(_SANDBOX_ERRORS))),
            own_executor=True,
        )
        self._prepare_deadline = min(
            time.monotonic() + _PREPARE_SECONDS,
            deadline if deadline is not None else float("inf"),
        )
        try:
            self._prepare(dom, source, module)
        except BaseException:
            self.close()
            raise

    def _prepare(self, dom: str, source: str, module: bool) -> None:
        self._context_call("set_memory_limit", _BUNDLE_MEMORY)
        self._context_call("set_time_limit", _CALL_SECONDS)
        for step, payload in (("eval", dom), ("boot", None)):
            self._limit_preparation()
            error = self.engine(step, payload).get("error")
            if error:
                raise ValueError(error)
        self._limit_preparation()
        if module:
            self._context_call("module", source)
        elif error := self.engine("eval", source).get("error"):
            raise ValueError(error)
        self._settle()
        self._limit_preparation()
        listed = self.engine("cards", None)["value"]
        self.tags: list[str] = listed["tags"]
        self.cards: list[dict[str, Any]] = listed["cards"]
        self._context_call("set_time_limit", _CALL_SECONDS)
        self._unresponsive: set[str] = set()
        self._prepared: set[str] = set()
        self._editor_unavailable: set[str] = set()

    def _prepare_tag(self, tag: str) -> None:
        """Load one editor without consuming the dashboard's whole check budget."""
        if tag in self._prepared:
            return
        self._prepare_deadline = time.monotonic() + _EDITOR_SECONDS
        try:
            self._limit_preparation()
            answer = self.engine("prepare", {"tag": tag})
            if error := answer.get("error"):
                raise ValueError(error)
            if answer.get("value"):
                self._settle()
        finally:
            self._context_call("set_time_limit", _CALL_SECONDS)
        self._prepared.add(tag)

    def _editor_ready(self, tag: str) -> bool:
        if tag in self._editor_unavailable:
            return False
        try:
            self._prepare_tag(tag)
            return True
        except Exception:
            _LOGGER.debug("Custom card editor %s did not load", tag, exc_info=True)
            self._editor_unavailable.add(tag)
            return False

    def close(self) -> None:
        """Dispose native objects on their creating thread before releasing capacity."""
        self.engine._threadpool.submit(self._dispose).result()
        self.engine._threadpool.shutdown(wait=True)

    def _dispose(self) -> None:
        del self.engine._f
        del self.engine._context

    def _context_call(self, method: str, *args: Any) -> Any:
        # The tested quickjs-ng wrapper dispatches Function calls, but its context methods
        # run on the caller. Use the SAME executor that created its runtime.
        target = self.engine._context if method == "module" else self.engine
        return self.engine._threadpool.submit(getattr(target, method), *args).result()

    def _limit_preparation(self) -> None:
        remaining = self._prepare_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Custom card preparation exceeded its budget")
        self._context_call("set_time_limit", min(_CALL_SECONDS, remaining))

    def _settle(self) -> None:
        """Run the bundle's queued promises and timers (lazy editors load here)."""
        deadline = time.monotonic() + _SETTLE_SECONDS
        jobs = 0
        while jobs < _SETTLE_JOBS and time.monotonic() < deadline:
            self._limit_preparation()
            if self._context_call("execute_pending_job"):
                jobs += 1
            elif not self.engine("pump", None).get("value"):
                return

    @property
    def memory(self) -> int:
        return int(self._context_call("memory").get("memory_used_size", 0))

    def check(self, tag: str, config: dict[str, Any]) -> list[dict[str, str]]:
        """The card's own objections; none (from then on) once it times out."""
        if tag in self._unresponsive:
            return []
        try:
            answer = self.engine(
                "check", {"tag": tag, "config": config, "source": "card"}
            )
        except Exception:
            _LOGGER.debug("Custom card %s did not answer", tag, exc_info=True)
            self._unresponsive.add(tag)
            return []
        problems = list(answer.get("value") or [])
        if self._editor_ready(tag):
            try:
                self._context_call("set_time_limit", _EDITOR_SECONDS)
                answer = self.engine(
                    "check", {"tag": tag, "config": config, "source": "editor"}
                )
                seen = {problem["message"] for problem in problems}
                problems.extend(
                    problem
                    for problem in answer.get("value") or []
                    if problem["message"] not in seen
                )
            except Exception:
                _LOGGER.debug(
                    "Custom card editor %s did not answer", tag, exc_info=True
                )
                self._editor_unavailable.add(tag)
            finally:
                self._context_call("set_time_limit", _CALL_SECONDS)
        return problems

    def form(self, tag: str) -> list[Any] | None:
        if not self._editor_ready(tag):
            return None
        try:
            value = self.engine(
                "form", {"tag": tag, "config": {"type": f"custom:{tag}"}}
            )
        except Exception:  # noqa: BLE001
            self._editor_unavailable.add(tag)
            return None
        form = value.get("value")
        return form if isinstance(form, list) else None


class CustomCards:
    """The custom cards the dashboard resources register, by element tag."""

    def __init__(self, dom: str) -> None:
        self._dom = dom
        self._bundles: dict[Path, tuple[float, _Bundle | None]] = {}
        self._modules: set[Path] = set()
        self._capacity_skipped: set[Path] = set()
        self._budget_deferred: set[Path] = set()
        self._lock = threading.Lock()
        self._refresh_deadline: float | None = None
        self._card_types: list[dict[str, Any]] = []
        self._skipped: dict[Path, str] = {}
        self._status: dict[str, Any] = {"state": "ready", "resources": []}

    def refresh(self, files: list[Path], modules: set[Path] | None = None) -> None:
        """Load new or changed resource files; drop removed ones."""
        # Readers retain a runtime until their native call finishes. Replacement
        # must wait, then dispose it before its heap reservation can be reused.
        with self._lock:
            self._refresh_deadline = time.monotonic() + _REFRESH_SECONDS
            try:
                self._refresh(files, modules)
            finally:
                self._card_types = [
                    {**card, "type": f"custom:{card['type']}"}
                    for _, bundle in self._bundles.values()
                    if bundle is not None
                    for card in bundle.cards
                    if isinstance(card.get("type"), str)
                ]
                self._refresh_deadline = None
                self._status = {
                    "state": "partial" if self._skipped else "ready",
                    "resources": [
                        {"resource": str(p), "reason": reason}
                        for p, reason in self._skipped.items()
                    ],
                }

    def _drop(self, path: Path) -> None:
        removed = self._bundles.pop(path, None)
        if removed is not None and removed[1] is not None:
            removed[1].close()

    def _refresh(self, files: list[Path], modules: set[Path] | None) -> None:
        assert self._refresh_deadline is not None
        module_paths = modules or set()
        for changed in self._modules ^ module_paths:
            self._drop(changed)
        self._modules = set(module_paths)
        self._capacity_skipped.intersection_update(files)
        self._budget_deferred.intersection_update(files)
        self._skipped = {p: r for p, r in self._skipped.items() if p in files}
        for gone in set(self._bundles) - set(files):
            self._drop(gone)
        attempted = 0
        for path in sorted(files, key=lambda p: p not in self._budget_deferred):
            if time.monotonic() >= self._refresh_deadline:
                if path not in self._bundles:
                    self._skipped[path] = "refresh budget; awaiting a later request"
                continue
            try:
                stat = path.stat()
            except OSError:
                self._drop(path)
                self._capacity_skipped.discard(path)
                self._budget_deferred.discard(path)
                self._skipped[path] = "resource file is missing or unreadable"
                continue
            if (
                path in self._bundles
                and self._bundles[path][0] == stat.st_mtime
                and path not in self._capacity_skipped
            ):
                continue
            self._drop(path)
            self._budget_deferred.discard(path)
            self._bundles[path] = (stat.st_mtime, self._load(path, stat.st_size))
            if (
                attempted
                and self._bundles[path][1] is None
                and time.monotonic() >= self._refresh_deadline
            ):
                # A late bundle got only the remainder of this pass. Retry it
                # first next time; failure with a full budget stays cached.
                self._bundles.pop(path)
                self._budget_deferred.add(path)
                self._skipped[path] = "refresh budget; awaiting a later request"
            attempted += 1

    def _load(self, path: Path, size: int) -> _Bundle | None:
        self._capacity_skipped.discard(path)
        self._skipped.pop(path, None)
        # Reserve each runtime's full allowance, including heap growth in later
        # check/form calls. Measuring current heaps only cannot enforce the cap.
        reserved = sum(
            _BUNDLE_MEMORY for _, b in self._bundles.values() if b is not None
        )
        if size > _MAX_SOURCE_BYTES:
            self._skipped[path] = "source size limit"
            _LOGGER.debug("Custom card bundle %s skipped: source size cap", path)
            return None
        if reserved + _BUNDLE_MEMORY > _TOTAL_MEMORY:
            self._capacity_skipped.add(path)
            self._skipped[path] = "memory limit"
            _LOGGER.debug("Custom card bundle %s skipped: memory cap", path)
            return None
        try:
            return _Bundle(
                self._dom,
                path.read_text(encoding="utf-8"),
                module=path in self._modules,
                deadline=self._refresh_deadline,
            )
        except Exception:
            self._skipped[path] = "bundle could not run in the inspection sandbox"
            _LOGGER.debug("Custom card bundle %s did not load", path, exc_info=True)
            return None

    def _owner(self, tag: str) -> _Bundle | None:
        for _, bundle in self._bundles.values():
            if bundle is not None and tag in bundle.tags:
                return bundle
        return None

    def check(self, tag: str, config: dict[str, Any]) -> list[dict[str, str]] | None:
        """Problems the card reports, or ``None`` when no loaded bundle defines it."""
        with self._lock:
            bundle = self._owner(tag)
            return None if bundle is None else bundle.check(tag, config)

    def card_types(self) -> list[dict[str, Any]]:
        # Called on HA's event loop: use published metadata without waiting for
        # the lock held by native work in an executor.
        return [dict(card) for card in self._card_types]

    def status(self) -> dict[str, Any]:
        """Published resource coverage; no wait on the native runtime lock."""
        return {
            "state": self._status["state"],
            "resources": [dict(r) for r in self._status["resources"]],
        }

    def describe(self, tag: str) -> dict[str, Any] | None:
        with self._lock:
            bundle = self._owner(tag)
            if bundle is None:
                return None
            listed = next((c for c in bundle.cards if c.get("type") == tag), {})
            fields = bundle.form(tag)
            result = {
                "type": f"custom:{tag}",
                "name": listed.get("name"),
                "description": listed.get("description"),
                "fields": fields,
            }
            if fields is not None:
                result.update(
                    field_coverage="partial",
                    note="Fields come from the custom card's editor form. They may include editor-only values and omit options accepted by the card; this is not a complete stored-config schema.",
                )
            return result


_dom_failed_at: float | None = None
_custom: CustomCards | None = None
_REFRESH_LOCK = asyncio.Lock()
_refresh_task: asyncio.Task[CustomCards | None] | None = None
_resource_error: str | None = None
_unsupported_resources: list[dict[str, str]] = []


def custom_load_status(custom: CustomCards | None) -> dict[str, Any]:
    status = (
        custom.status()
        if custom is not None
        else {"state": "unavailable", "resources": []}
    )
    if _refresh_task is not None and not _refresh_task.done():
        status["state"] = "loading"
    elif _resource_error:
        status.update(state="unavailable", reason=_resource_error)
    return status


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
            tarball = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                if len(tarball) + len(chunk) > _MAX_DOM_DOWNLOAD_BYTES:
                    raise ValueError("linkedom download exceeded its size limit")
                tarball.extend(chunk)
        dom = await hass.async_add_executor_job(dom_script, bytes(tarball))
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


async def _async_resource_files(hass: HomeAssistant) -> dict[Path, bool]:
    from .websocket_api.dashboards import _lovelace_container

    resources = getattr(_lovelace_container(hass), "resources", None)
    if resources is None:
        _unsupported_resources.clear()
        return {}
    await resources.async_get_info()  # loads a storage collection on first use
    items = resources.async_items()
    files: dict[Path, bool] = await hass.async_add_executor_job(
        _resource_files, Path(hass.config.config_dir), list(items or [])
    )
    return files


def _resource_files(config_dir: Path, items: list[dict[str, Any]]) -> dict[Path, bool]:
    """Resolve paths off the event loop, including symlink/filesystem checks."""
    files = {}
    _unsupported_resources.clear()
    for item in items or []:
        if item.get("type") in ("module", "js") and isinstance(item.get("url"), str):
            if (path := resource_path(config_dir, item["url"])) is not None:
                files[path] = item["type"] == "module"
            else:
                _unsupported_resources.append(
                    {
                        "resource": item["url"],
                        "reason": "only local JavaScript resources can be inspected",
                    }
                )
    return files


async def async_get_custom_cards(
    hass: HomeAssistant, timeout: float | None = None
) -> CustomCards | None:
    """Current resources, retaining published metadata during a slow refresh.

    Callers share a refresh that outlives their timeout instead of queuing more.
    """
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = hass.async_create_background_task(
            _async_refresh(hass), "ha_mcp_tools custom cards"
        )
    try:
        return await asyncio.wait_for(asyncio.shield(_refresh_task), timeout)
    except TimeoutError:
        return _custom


async def _async_refresh(hass: HomeAssistant) -> CustomCards | None:
    global _custom, _resource_error
    async with _REFRESH_LOCK:
        try:
            files = await _async_resource_files(hass)
            _resource_error = None
            if _custom is None or (files and not _custom._dom):
                dom = await _async_dom(hass) if files else ""
                if dom is None:
                    _resource_error = "linkedom could not be downloaded or loaded"
                    return _custom
                _custom = CustomCards(dom)
            await hass.async_add_executor_job(
                _custom.refresh,
                list(files),
                {path for path, module in files.items() if module},
            )
            if _unsupported_resources:
                _custom._status = {
                    "state": "partial",
                    "resources": [
                        *_custom.status()["resources"],
                        *_unsupported_resources,
                    ],
                }
        except Exception:
            _resource_error = "dashboard resources could not be inspected"
            _LOGGER.debug("Custom cards are unavailable", exc_info=True)
        return _custom
