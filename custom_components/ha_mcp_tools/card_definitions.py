"""Home Assistant's own Lovelace card definitions, read from the frontend on disk (issue #2632).

Core stores dashboard config unchecked; the only definition of a card lives in
the frontend Core serves (the ``hass_frontend`` package). Each card editor
there carries the form the UI renders (an ha-form schema) and the struct its
``setConfig`` asserts. Both are JavaScript expressions in the built files, so
an embedded QuickJS engine evaluates just those expressions: no browser, no
DOM. Only Home Assistant's own installed frontend code runs; dashboard configs
reach the engine as JSON data.
"""

from __future__ import annotations

import asyncio
import bisect
import difflib
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .custom_cards import CustomCards, async_get_custom_cards

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WS_DASHBOARD_CARDS = "ha_mcp_tools/dashboard_cards"
CAPABILITIES = ("dashboard_cards",)

# A module factory takes (module, exports, require), trailing ones omitted when unused.
_MOD_RE = re.compile(r"[{,](\d+)\([\w$]+(?:,[\w$]+){0,2}\)\{")
# Entrypoints end their registry with the module cache and require function.
_ENTRYPOINT_END_RE = re.compile(r"\},[\w$]+=\{\};function [\w$]+\(")
# The element registration, ``(0,x.EM)("hui-tile-card")``; never a createElement.
_TAG_RE = re.compile(r'\)\("(hui-[a-z0-9-]+-card(?:-editor)?)"\)')
_EDITOR_REF_RE = re.compile(r'"(hui-[a-z0-9-]+-card-editor)"')
# ``m=a(80140)``, or ``_=(a(47551),a(20541))`` where the last id is the binding.
_ALIAS_RE = re.compile(r"([\w$]+)=\(?(?:[\w$]+\(\d+\),)*[\w$]+\((\d+)\)")
# The editor asserts its struct on the raw config; an editor that migrates the
# config first is skipped, since the raw config would draw false warnings.
_STRUCT_RE = re.compile(
    r"setConfig\(([\w$]+)\)\{[^}]*?\(0,[\w$]+\.[\w$]+\)\(([\w$.]+),([\w$]+)\)"
)
_SCHEMA_FN_RE = re.compile(r"_schema=\(0,[\w$]+\.[\w$]+\)\(")
_SCHEMA_CONST_RE = re.compile(r"\.schema=\$\{([\w$]+)\}")
_REF_ERROR_RE = re.compile(r"ReferenceError: ([\w$]+) is not defined")
_I18N = "ui.panel.lovelace.editor.card."
_MAX_WARNINGS = 20
# Leave room for save/readback and transport within the 30-second command wait.
_CARD_WORK_SECONDS = 20.0
_CUSTOM_WAIT_SECONDS = 5.0
_Queued = list[tuple[str, str, dict[str, Any]]]
# Keys a card ignores but an installed plugin reads (card-mod); the editor
# rejects them only because it cannot show them.
_ACCEPTED_EXTRAS = frozenset({"card_mod"})

_ENGINE_JS = r"""
var REG = {}, CACHE = {}, STRUCTS = {};
var ANY = new Proxy(function () {}, {
  get: function (t, k) {
    if (k === Symbol.toPrimitive) return function () { return ''; };
    return k === 'then' ? undefined : ANY;
  },
  apply: function () { return ANY; },
});
function load(id) {
  if (!REG[id]) {
    var src = __chunk(id);
    if (src) {
      var holder = {};
      new Function('exports_', src)(holder);
      var mods = holder.__webpack_modules__ || {};
      for (var k in mods) if (!REG[k]) REG[k] = mods[k];
    }
    // An absent module cannot provide browser-only dependencies.
    if (!REG[id]) REG[id] = function () {};
  }
}
function req(id) {
  id = String(id);
  if (CACHE[id]) return CACHE[id].exports;
  load(id);
  var module = (CACHE[id] = { exports: {} });
  try { REG[id](module, module.exports, req); }
  catch (e) { delete CACHE[id]; throw e; }
  return module.exports;
}
req.d = function (e, getters, values) {
  for (var k in getters || {}) Object.defineProperty(e, k, { enumerable: true, get: getters[k] });
  for (var v in values || {}) Object.defineProperty(e, v, { enumerable: true, value: values[v] });
};
req.r = function () {};
req.n = function (m) { return function () { return m; }; };
req.o = function (o, k) { return Object.prototype.hasOwnProperty.call(o, k); };
req.g = globalThis;
function build(src, bindings) {
  var names = Object.keys(bindings);
  var values = names.map(function (n) {
    var b = bindings[n];
    if (b === 'any') return ANY;
    try { return req(b); } catch (e) { throw new Error('require-failed ' + n + ' module ' + b + ': ' + e); }
  });
  // Editor forms may read this.hass; an inert stand-in keeps them pure.
  return Function.apply(null, names.concat([src])).apply(ANY, values);
}
function plain(v) {
  return JSON.parse(JSON.stringify(v, function (k, x) { return typeof x === 'function' ? undefined : x; }));
}
function engine(op, p) {
  try {
    if (op === 'source') {
      load(String(p.id));
      return { value: REG[String(p.id)].toString() };
    }
    if (op === 'schema') {
      var fn = build(p.src, p.bindings);
      // The first argument is localize or a hass-like object carrying it.
      var localize = function (k) { return k; };
      var first = new Proxy(localize, {
        get: function (t, k) { return k === 'localize' ? localize : ANY; },
      });
      var v = typeof fn === 'function' ? fn(first, false, false, false) : fn;
      return { value: plain(v) };
    }
    if (op === 'struct') {
      STRUCTS[p.key] = build(p.src, p.bindings);
      return { value: Object.keys(STRUCTS[p.key].schema || {}) };
    }
    if (op === 'validate') {
      return { value: p.cards.map(function (c) {
        var s = STRUCTS[c.key];
        if (!s) return [];
        var r = s.validate(c.config);
        if (!r[0]) return [];
        return r[0].failures().slice(0, 5).map(function (f) {
          return { path: f.path, type: f.type, message: f.message };
        });
      }) };
    }
    return { error: 'unknown op' };
  } catch (e) {
    return { error: String(e) };
  }
}
"""


def _balanced_end(s: str, i: int) -> int:
    """Index just past the bracket group that opens at ``s[i]``."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    j = i
    while j < len(s):
        c = s[j]
        if c in "\"'`":
            j = _string_end(s, j)
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return -1


def _string_end(s: str, i: int) -> int:
    quote, j = s[i], i + 1
    while j < len(s) and s[j] != quote:
        j += 2 if s[j] == "\\" else 1
    return j + 1


def _expr_end(s: str, i: int) -> int:
    """End of the expression at ``i``: the next top-level ``,`` or ``;``."""
    j = i
    while j < len(s):
        c = s[j]
        if c in "([{":
            j = _balanced_end(s, j)
            if j < 0:
                return len(s)
            continue
        if c in "\"'`":
            j = _string_end(s, j)
            continue
        if c in ",;":
            return j
        j += 1
    return j


def _local_definition(body: str, name: str) -> str | None:
    m = re.search(r"[,;\s{(]" + re.escape(name) + r"=(?!=)", body)
    if not m:
        return None
    return body[m.end() : _expr_end(body, m.end())]


class CardDefinitions:
    """Card types, forms and validators from one installed frontend build."""

    def __init__(self, root: Path) -> None:
        import quickjs

        self._dir = root / "frontend_latest"
        self._module_file: dict[str, Path] = {}
        self._tag_module: dict[str, str] = {}  # element tag -> module id
        self._bodies: dict[str, str] = {}  # module id -> source
        self._card_types: set[str] = set()
        self._struct_keys: dict[str, list[str] | None] = {}
        self._index()
        self._strings = _load_strings(root)
        self._engine = quickjs.Function("engine", _ENGINE_JS, own_executor=True)
        self._engine._threadpool.submit(
            self._engine.set_memory_limit, 256 * 1024 * 1024
        ).result()
        self._engine._threadpool.submit(
            self._engine.add_callable, "__chunk", self._chunk_source
        ).result()

    def _index(self) -> None:
        files = sorted(self._dir.glob("*.js"), key=lambda p: p.stat().st_size)
        for path in files:
            text = path.read_text(encoding="utf-8")
            starts = [(m.start(), m.group(1)) for m in _MOD_RE.finditer(text)]
            for _, module_id in starts:
                self._module_file.setdefault(module_id, path)
            for m in _TAG_RE.finditer(text):
                owner = bisect.bisect_left(starts, (m.start(), "")) - 1
                if owner >= 0:
                    self._tag_module.setdefault(m.group(1), starts[owner][1])
        for tag in self._tag_module:
            if not tag.endswith("-card-editor"):
                self._card_types.add(tag[len("hui-") : -len("-card")])

    def _body(self, tag: str) -> str:
        """A module's source as the engine parsed it (exact, unlike a scan)."""
        module_id = self._tag_module.get(tag)
        if module_id is None:
            return ""
        if module_id not in self._bodies:
            self._bodies[module_id] = self._engine("source", {"id": module_id}).get(
                "value", ""
            )
        return self._bodies[module_id]

    def _editor(self, card_type: str) -> str:
        """The editor tag the card's ``getConfigElement`` creates."""
        tag = f"hui-{card_type}-card-editor"
        if tag in self._tag_module:
            return tag
        ref = _EDITOR_REF_RE.search(self._body(f"hui-{card_type}-card"))
        return ref.group(1) if ref else ""

    def _chunk_source(self, module_id: str) -> str | None:
        path = self._module_file.get(module_id)
        if path is None:
            return None
        text = path.read_text(encoding="utf-8")
        if "__webpack_modules__=" in text:
            return text.replace("export const ", "exports_.")
        # core/app entrypoints keep dependencies in a local registry instead
        # of exporting a chunk. Capture that object without executing startup.
        factories = list(_MOD_RE.finditer(text))
        if not factories or text[factories[0].start()] != "{":
            return None
        end = _ENTRYPOINT_END_RE.search(text, factories[-1].end())
        if end is None:
            return None
        # Exclude runtime code entirely: its import.meta is module-only syntax,
        # even when an early return would prevent browser startup execution.
        return (
            "exports_.__webpack_modules__="
            + text[factories[0].start() : end.start() + 1]
            + ";"
        )

    def _evaluate(self, op: str, body: str, expr: str, key: str = "") -> Any:
        """Run ``expr`` from ``body``, binding the module's imports and locals."""
        aliases = dict(_ALIAS_RE.findall(body))
        bindings: dict[str, str] = {}
        prelude: list[str] = []
        for _ in range(40):
            src = "".join(prelude) + "return (" + expr + ")"
            out = self._engine(op, {"src": src, "bindings": bindings, "key": key})
            error = out.get("error")
            if error is None:
                return out["value"]
            # A form may leave a name inert; a validator must not, or it
            # would flag valid configs.
            inert_ok = op == "schema"
            failed = re.match(r"Error: require-failed ([\w$]+)", error)
            if failed and inert_ok:
                bindings[failed.group(1)] = "any"
                continue
            missing = _REF_ERROR_RE.match(error)
            if not missing:
                raise ValueError(error)
            name = missing.group(1)
            if name in aliases and name not in bindings:
                bindings[name] = aliases[name]
            elif (definition := _local_definition(body, name)) is not None:
                prelude.insert(0, f"var {name}={definition};")
            elif inert_ok:
                bindings[name] = "any"
            else:
                raise ValueError(f"unresolved {name}")
        raise ValueError("unresolved references")

    def card_types(self) -> list[dict[str, Any]]:
        listed = []
        for card_type in sorted(self._card_types):
            name = self._strings.get(f"{_I18N}{card_type}.name")
            if name is None:
                continue
            listed.append(
                {
                    "type": card_type,
                    "name": name,
                    "description": self._strings.get(f"{_I18N}{card_type}.description"),
                }
            )
        return listed

    def describe(self, card_type: str) -> dict[str, Any] | None:
        if card_type not in self._card_types:
            return None
        result: dict[str, Any] = {
            "type": card_type,
            "name": self._strings.get(f"{_I18N}{card_type}.name"),
            "description": self._strings.get(f"{_I18N}{card_type}.description"),
            "fields": None,
        }
        body = self._body(self._editor(card_type))
        for expr in _schema_expressions(body):
            try:
                value = self._evaluate("schema", body, expr)
            except ValueError:
                continue
            if isinstance(value, list):
                result["fields"] = self._with_help(card_type, value)
                break
        return result

    def _with_help(self, card_type: str, fields: list[Any]) -> list[Any]:
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            if name and "description" not in field:
                help_text = self._strings.get(
                    f"{_I18N}{card_type}.{name}_helper"
                ) or self._strings.get(f"{_I18N}generic.{name}_helper")
                if help_text:
                    field["description"] = help_text
            if isinstance(field.get("schema"), list):
                self._with_help(card_type, field["schema"])
        return fields

    def _struct_ready(self, card_type: str) -> bool:
        if card_type not in self._struct_keys:
            body = self._body(self._editor(card_type))
            match = _STRUCT_RE.search(body)
            definition = (
                _local_definition(body, match.group(3))
                if match and match.group(1) == match.group(2)
                else None
            )
            keys = None
            if definition:
                try:
                    keys = self._evaluate("struct", body, definition, key=card_type)
                except ValueError as exc:
                    _LOGGER.debug(
                        "Card validator %s is unavailable: %s", card_type, exc
                    )
            self._struct_keys[card_type] = keys
        return self._struct_keys[card_type] is not None

    def validate(
        self, config: dict[str, Any], custom: CustomCards | None = None
    ) -> list[str]:
        """One warning per problem the frontend would flag in a stored card."""
        warnings, checked, customs = self._triage(config)
        if custom is not None:
            warnings.extend(_custom_warnings(custom, customs))
        if checked:
            payload = [{"key": t, "config": c} for _, t, c in checked]
            results = self._engine("validate", {"cards": payload}).get("value") or []
            for (path, card_type, _), failures in zip(checked, results, strict=False):
                explained = (self._explain(card_type, f) for f in failures)
                warnings.extend(f"{path} ({card_type}): {e}" for e in explained if e)
        if len(warnings) > _MAX_WARNINGS:
            more = len(warnings) - _MAX_WARNINGS
            warnings = [*warnings[:_MAX_WARNINGS], f"...and {more} more card problems"]
        return warnings

    def _triage(self, config: dict[str, Any]) -> tuple[list[str], _Queued, _Queued]:
        """Flag missing or unknown types; queue the cards a validator can check."""
        warnings: list[str] = []
        checked: _Queued = []
        customs: _Queued = []
        for path, card in _cards(config):
            card_type = card.get("type")
            if not isinstance(card_type, str):
                warnings.append(f"{path}: no card type configured")
            elif card_type.startswith("custom:"):
                customs.append((path, card_type, card))
            elif card_type not in self._card_types:
                warnings.append(f"{path}: unknown card type '{card_type}'")
            elif self._struct_ready(card_type):
                checked.append((path, card_type, card))
        return warnings, checked, customs

    def _explain(self, card_type: str, failure: dict[str, Any]) -> str | None:
        path = ".".join(str(p) for p in failure.get("path") or [])
        if path in _ACCEPTED_EXTRAS:
            return None
        if failure.get("type") == "never" and len(failure.get("path") or []) == 1:
            message = f"'{path}' is not a {card_type} card option"
            close = difflib.get_close_matches(
                path, self._struct_keys.get(card_type) or [], n=1
            )
            return f"{message}; did you mean '{close[0]}'?" if close else message
        message = re.sub(r"^At path: \S+ -- ", "", failure.get("message", ""))
        return f"{path}: {message}" if path else message


def _custom_warnings(custom: CustomCards, customs: _Queued) -> list[str]:
    """What each custom card says about its own config."""
    found: list[str] = []
    deadline = time.monotonic() + _CUSTOM_WAIT_SECONDS
    for path, card_type, card in customs:
        if time.monotonic() >= deadline:
            break
        tag = card_type[len("custom:") :]
        explained = (_explain_message(tag, m) for m in custom.check(tag, card) or [])
        found.extend(f"{path} ({card_type}): {e}" for e in explained if e is not None)
    return found


def _explain_message(tag: str, message: str) -> str | None:
    """A custom card's own error, with a struct's unknown-key wording made plain."""
    unknown = re.match(r"At path: (\w+) -- Expected a value of type `never`", message)
    if unknown:
        key = unknown.group(1)
        return (
            None if key in _ACCEPTED_EXTRAS else f"'{key}' is not a {tag} card option"
        )
    return re.sub(r"^At path: (\S+) -- ", r"\1: ", message)


def _schema_expressions(body: str) -> list[str]:
    """The editor's form: a memoized ``_schema`` function or a module constant."""
    found = []
    for m in _SCHEMA_FN_RE.finditer(body):
        end = _balanced_end(body, m.end() - 1)
        if end > 0:
            found.append(body[m.end() : end - 1])
    for m in _SCHEMA_CONST_RE.finditer(body):
        definition = _local_definition(body, m.group(1))
        if definition and definition.startswith("["):
            found.append(definition)
    return found


def _load_strings(root: Path) -> dict[str, str]:
    """The English lovelace strings (card names, descriptions, field help)."""
    for path in (root / "static" / "translations" / "lovelace").glob("en-*.json"):
        if re.fullmatch(r"en-[0-9a-f]+\.json", path.name):
            data = json.loads(path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if k.startswith(_I18N)}
    return {}


def _walk_card(
    path: str,
    card: Any,
    found: list[tuple[str, dict[str, Any]]],
    depth: int = 0,
    *,
    container: bool = False,
) -> None:
    if not isinstance(card, dict) or depth > 50:
        return
    if not container or "type" in card:
        found.append((path, card))
    children = card.get("cards")
    for i, child in enumerate(children if isinstance(children, list) else []):
        _walk_card(f"{path}.cards[{i}]", child, found, depth + 1)
    # entity-filter's ``card`` holds options for its rows, not a card.
    if (
        card.get("type") == "conditional"
        or str(card.get("type", "")).startswith("custom:")
        or container
    ):
        _walk_card(f"{path}.card", card.get("card"), found, depth + 1)
    # Match the search walk's named containers; field wrappers are not cards.
    for key in ("custom_fields", "states"):
        named = card.get(key)
        if isinstance(named, dict):
            for name, child in named.items():
                if isinstance(name, str):
                    _walk_card(
                        f"{path}.{key}[{json.dumps(name)}]",
                        child,
                        found,
                        depth + 1,
                        container=key == "custom_fields",
                    )


def _cards(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every card position the frontend renders, nested stacks included."""
    found: list[tuple[str, dict[str, Any]]] = []
    views = config.get("views")
    for v, view in enumerate(views if isinstance(views, list) else []):
        if not isinstance(view, dict):
            continue
        header = view.get("header")
        if isinstance(header, dict):
            _walk_card(f"views[{v}].header.card", header.get("card"), found)
        for i, card in enumerate(view.get("cards") or []):
            _walk_card(f"views[{v}].cards[{i}]", card, found)
        for s, section in enumerate(view.get("sections") or []):
            cards = section.get("cards") if isinstance(section, dict) else None
            for i, card in enumerate(cards or []):
                _walk_card(f"views[{v}].sections[{s}].cards[{i}]", card, found)
    return found


_definitions: CardDefinitions | None = None
_build_task: asyncio.Task[CardDefinitions | None] | None = None


def _build() -> CardDefinitions | None:
    try:
        import hass_frontend

        return CardDefinitions(hass_frontend.where())
    except Exception:
        _LOGGER.warning("Card definitions are unavailable", exc_info=True)
        return None


async def async_get_definitions(
    hass: HomeAssistant, timeout: float | None = None
) -> CardDefinitions | None:
    """The frontend's card definitions; built once per Home Assistant run."""
    global _build_task, _definitions
    if _definitions is not None:
        return _definitions
    task = _build_task
    if task is None:

        async def _run() -> CardDefinitions | None:
            built: CardDefinitions | None = await hass.async_add_executor_job(_build)
            return built

        task = _build_task = hass.async_create_background_task(
            _run(), "ha_mcp_tools card definitions"
        )
    try:
        _definitions = await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        return None
    return _definitions


def async_warm_up(hass: HomeAssistant) -> None:
    """Load the card definitions once Home Assistant has started.

    A first dashboard write after a restart would otherwise pay the index
    build and custom-card loading, and drop the checks that missed its wait.
    """

    async def _warm() -> None:
        if await async_get_definitions(hass) is not None:
            await async_get_custom_cards(hass)

    def _start(_event: Any = None) -> None:
        hass.async_create_background_task(_warm(), "ha_mcp_tools card warm-up")

    if getattr(hass, "is_running", False) is True:
        _start()
    elif (bus := getattr(hass, "bus", None)) is not None:
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
        from homeassistant.core import callback

        # A plain listener runs on a worker thread; the task must start on the loop.
        bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, callback(_start))


async def async_card_warnings(hass: HomeAssistant, config: dict[str, Any]) -> list[str]:
    """Advisory warnings for a saved dashboard; never fails the write."""
    try:
        async with asyncio.timeout(_CARD_WORK_SECONDS):
            definitions = await async_get_definitions(hass, timeout=15)
            if definitions is None:
                return []
            uses_custom = any(
                str(card.get("type", "")).startswith("custom:")
                for _, card in _cards(config)
            )
            custom = (
                await async_get_custom_cards(hass, timeout=_CUSTOM_WAIT_SECONDS)
                if uses_custom
                else None
            )
            warnings: list[str] = await hass.async_add_executor_job(
                definitions.validate, config, custom
            )
            return warnings
    except Exception:
        _LOGGER.debug("Card validation failed", exc_info=True)
        return []


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """``dashboard_cards``: the card type list, or one card type's form."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(_CARD_WORK_SECONDS):
                return await describe(hass, msg)
        except TimeoutError:
            return {"result": {"success": False, "error": "unavailable"}}

    async def describe(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        definitions = await async_get_definitions(hass)
        if definitions is None:
            return {"result": {"success": False, "error": "unavailable"}}
        custom = await async_get_custom_cards(hass, timeout=_CUSTOM_WAIT_SECONDS)
        card_types = definitions.card_types() + (custom.card_types() if custom else [])
        card_type = msg.get("card_type")
        if card_type is None:
            return {"result": {"success": True, "card_types": card_types}}
        if card_type.startswith("custom:"):
            describe = custom.describe if custom else lambda _tag: None
            described = await hass.async_add_executor_job(
                describe, card_type[len("custom:") :]
            )
        else:
            described = await hass.async_add_executor_job(
                definitions.describe, card_type
            )
        if described is None:
            return {
                "result": {
                    "success": False,
                    "error": "unknown_card_type",
                    "card_types": [c["type"] for c in card_types],
                }
            }
        return {"result": {"success": True, **described}}

    def do(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_DASHBOARD_CARDS,
                vol.Optional("card_type"): str,
            },
            do,
            prep,
        )
    ]
