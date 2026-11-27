"""The installed frontend's own JavaScript, run in an embedded QuickJS engine.

Some Home Assistant rules and data live only in the frontend Core serves (the
``hass_frontend`` package): card editor forms and validators, the energy meter
suggestions. This module indexes that build's webpack modules and evaluates one
expression or exported function from them, binding the module's imports and
locals, with no browser and no DOM. Only Home Assistant's own installed
frontend code runs; Home Assistant data reaches the engine as JSON.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from .error_wording import missing_reference

# A module factory takes (module, exports, require), trailing ones omitted when unused.
_MODULE_ID = r"\d+(?:e\d+)?"
_MOD_RE = re.compile(r"[{,](" + _MODULE_ID + r")\([\w$]+(?:,[\w$]+){0,2}\)\{")
# Entrypoints end their registry with the module cache and require function.
_ENTRYPOINT_END_RE = re.compile(r"\},[\w$]+=\{\};function [\w$]+\(")
# ``m=a(80140)``, or ``_=(a(47551),a(20541))`` where the last id is the binding.
_ALIAS_RE = re.compile(
    rf"([\w$]+)=\(?(?:[\w$]+\({_MODULE_ID}\),)*[\w$]+\(({_MODULE_ID})\)"
)

_ENGINE_JS = r"""
var REG = {}, CACHE = {}, STRUCTS = {}, PURE = {}, MODS = {};
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
// An async module (frontend 20261028+ tile editor forms) only awaits a
// dependency that is a promise; handing back the loaded ones runs it in sync.
req.a = function (module, body) {
  var failure;
  body(function (deps) { return deps; }, function (e) { failure = e; });
  if (failure) throw failure;
};
function build(src, bindings) {
  var names = Object.keys(bindings);
  var values = names.map(function (n) {
    var b = bindings[n];
    if (b === 'any') return ANY;
    try { return req(b); } catch (e) {
      return new Proxy({}, { get: function (t, k) {
        var key = b + ':' + String(k);
        if (Object.prototype.hasOwnProperty.call(PURE, key)) return PURE[key];
        var needed = new Error('export-needed ' + key);
        if (typeof k === 'string' && /^[\w$]+$/.test(k)) needed.exportNeeded = [b, k];
        throw needed;
      } });
    }
  });
  // Editor forms may read this.hass; an inert stand-in keeps them pure.
  return Function.apply(null, names.concat([src])).apply(ANY, values);
}
// A module's import, read export by export: a pure export Python has
// evaluated, else an export-needed error naming it.
function lazy(id) {
  return new Proxy({}, { get: function (t, k) {
    if (typeof k !== 'string' || k === 'then' || k === '__esModule') return undefined;
    var key = id + ':' + k;
    if (Object.prototype.hasOwnProperty.call(PURE, key)) return PURE[key];
    var needed = new Error('export-needed ' + key);
    if (/^[\w$]+$/.test(k)) needed.exportNeeded = [id, k];
    throw needed;
  } });
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
      var v = fn;
      if (typeof fn === 'function') {
        try { v = fn(first); }
        catch (e) {
          // Some forms require selected options even without an entity.
          // Preserve normal defaults in forms that work without this context.
          v = fn(first, undefined, []);
        }
      }
      return { value: plain(v) };
    }
    if (op === 'struct') {
      STRUCTS[p.key] = build(p.src, p.bindings);
      return { value: Object.keys(STRUCTS[p.key].schema || {}) };
    }
    if (op === 'export') {
      PURE[p.key] = build(p.src, p.bindings);
      return { value: true };
    }
    if (op === 'module') {
      // Run the module's own factory; its imports resolve export by export.
      load(String(p.id));
      var module = { exports: {} };
      var lazyReq = function (id) { return lazy(String(id)); };
      ['d', 'r', 'n', 'o', 'g', 'a'].forEach(function (k) { lazyReq[k] = req[k]; });
      REG[String(p.id)](module, module.exports, lazyReq);
      MODS[String(p.id)] = module.exports;
      return { value: Object.keys(module.exports) };
    }
    if (op === 'call') {
      var f = MODS[String(p.id)][p.name];
      return { value: plain(typeof f === 'function' ? f.apply(null, p.args) : f) };
    }
    if (op === 'field_types') {
      function fields(s, depth) {
        var out = {};
        if (!s || !s.schema || depth > 4) return out;
        Object.keys(s.schema).forEach(function (key) {
          var child = s.schema[key];
          if (!child || typeof child.type !== 'string') return;
          out[key] = { type: child.type };
          if ((child.type === 'object' || child.type === 'type') && child.schema) {
            out[key].schema = fields(child, depth + 1);
          }
        });
        return out;
      }
      return { value: fields(STRUCTS[p.key], 0) };
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
    var failed = { error: String(e) };
    if (e && e.exportNeeded) failed.export_needed = e.exportNeeded;
    return failed;
  }
}
"""


def _module_id(literal: str) -> str:
    """JavaScript indexes a factory written as 95e3 by the key 95000."""
    return str(int(Decimal(literal))) if "e" in literal else literal


def _aliases(body: str) -> dict[str, str]:
    return {name: _module_id(key) for name, key in _ALIAS_RE.findall(body)}


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


# An async module reads its dependency inline, ``(t=(i.then?(await i)():i)[0])``;
# the engine settles dependencies synchronously, so that is the binding ``t``.
_ASYNC_DEPENDENCY_RE = re.compile(
    r"\(([\w$]+)=\(([\w$]+)\.then\?\(await \2\)\(\):\2\)\[0\]\)"
)


def _local_definition(body: str, name: str) -> str | None:
    m = re.search(r"[,;\s{(]" + re.escape(name) + r"=(?![=>])", body)
    if not m:
        return None
    return _ASYNC_DEPENDENCY_RE.sub(r"\1", body[m.end() : _expr_end(body, m.end())])


_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
# Names the expression binds itself: parameters of its arrows (a default value
# included, ``(t,r={})=>``) and its own declarations.
_BOUND_RE = re.compile(
    r"(?<![\w$.])([\w$]+)=>|(?:const|let|var)\s+([\w$]+)|\(([^()]*)\)=>"
)


def _bound_names(code: str) -> set[str]:
    names: set[str] = set()
    for single, declared, params in _BOUND_RE.findall(code):
        names.update(n for n in (single, declared) if n)
        names.update(part.split("=")[0].strip() for part in params.split(","))
    return names - {""}


def _closure_locals(expr: str) -> list[str]:
    """Free names in a function expression, minus the names it binds and keys."""
    code = _STRING_RE.sub('""', expr)
    skip = _bound_names(code)
    names = []
    for m in re.finditer(r"(?<![\w$.])[A-Za-z_$][\w$]*", code):
        # An object key sits between "{" or "," and ":".
        before, after = code[m.start() - 1 : m.start()], code[m.end() : m.end() + 1]
        key = before in ("{", ",") and after == ":"
        if not key and m.group() not in skip:
            skip.add(m.group())
            names.append(m.group())
    return names


def _closure_prelude(body: str, expr: str, aliases: dict[str, str]) -> list[str]:
    return [
        f"var {name}={definition};"
        for name in _closure_locals(expr)
        if name not in aliases and (definition := _local_definition(body, name))
    ]


class FrontendBuild:
    """The webpack modules of one installed frontend build and an engine to run them."""

    def __init__(self, root: Path) -> None:
        import quickjs

        self._dir = root / "frontend_latest"
        self._module_file: dict[str, Path] = {}
        self._bodies: dict[str, str] = {}  # module id -> source
        self._modules: set[str] = set()
        self._found: dict[tuple[str, ...], str | None] = {}
        self._index()
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
            starts = [
                (m.start(), _module_id(m.group(1))) for m in _MOD_RE.finditer(text)
            ]
            for _, module_id in starts:
                self._module_file.setdefault(module_id, path)
            self._index_file(text, starts)

    def _index_file(self, text: str, starts: list[tuple[int, str]]) -> None:
        """Collect what a subclass needs from a build file while it is indexed."""

    def module_containing(self, *markers: str) -> str | None:
        """The id of the first module whose source holds every marker."""
        if markers not in self._found:
            self._found[markers] = self._find_module(markers)
        return self._found[markers]

    def _find_module(self, markers: tuple[str, ...]) -> str | None:
        for path in sorted(set(self._module_file.values())):
            text = path.read_text(encoding="utf-8")
            at = text.find(markers[0])
            while at >= 0:
                starts = list(_MOD_RE.finditer(text, 0, at))
                if starts:
                    module_id = _module_id(starts[-1].group(1))
                    body = self._module_body(module_id)
                    if all(marker in body for marker in markers):
                        return module_id
                at = text.find(markers[0], at + 1)
        return None

    def module_exports(self, module_id: str) -> dict[str, str]:
        """A module's exports: each exported name with the local definition it binds."""
        body = self._module_body(module_id)
        return {
            name: definition
            for m in re.finditer(r"\.d\([\w$]+,\{[^}]*\}(?:,\{[^}]*\})?\)", body)
            for name, local in re.findall(
                r"(?<![\w$])([\w$]+):(?:\(\)=>)?([\w$]+)", m.group(0)
            )
            if (definition := _local_definition(body, local)) is not None
        }

    def run_export(self, module_id: str, name: str, *args: Any) -> Any:
        """Call the export ``name`` of ``module_id`` with JSON arguments, or read
        an exported value. The module's own factory runs once, so its exports
        keep their closures; an import is resolved when it is first read."""
        for _ in range(40):
            if module_id in self._modules:
                out = self._js("call", {"id": module_id, "name": name, "args": [*args]})
            else:
                out = self._js("module", {"id": module_id})
                if "error" not in out:
                    self._modules.add(module_id)
                    continue
            if "error" not in out:
                return out["value"]
            needed = out.get("export_needed")
            if not needed:
                raise ValueError(out["error"])
            self._resolve_export(needed[0], needed[1], {}, 0, inert_ok=False)
        raise ValueError("unresolved references")

    def _js(self, op: str, payload: Any) -> Any:
        # run_gc=True (quickjs-ng's default) calls Context.gc() after every call:
        # Python's full gc.collect() and QuickJS's JS_RunGC(), under the GIL. Over
        # Home Assistant's heap that stalls the event loop (#2744). QuickJS still
        # runs JS_RunGC() itself when its allocations pass the GC threshold.
        return self._engine(op, payload, run_gc=False)

    def _module_body(self, module_id: str) -> str:
        if module_id not in self._bodies:
            self._bodies[module_id] = self._js("source", {"id": module_id}).get(
                "value", ""
            )
        return self._bodies[module_id]

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

    def _evaluate(
        self, op: str, body: str, expr: str, key: str = "", depth: int = 0
    ) -> Any:
        """Run ``expr`` from ``body``, binding the module's imports and locals."""
        aliases = _aliases(body)
        if depth > 10:
            raise ValueError("export dependency depth exceeded")
        # Functions retain their import bindings even when invoked later by a
        # different module. Only imports mentioned in this expression are needed.
        bindings = {
            name: module
            for name, module in aliases.items()
            if re.search(r"(?<![\w$.])" + re.escape(name) + r"\.", expr)
        }
        # An exported function reads module locals only when called, after
        # this source is gone, so bind them now.
        prelude = _closure_prelude(body, expr, aliases) if op == "export" else []
        for _ in range(40):
            src = "".join(prelude) + "return (" + expr + ")"
            out = self._js(op, {"src": src, "bindings": bindings, "key": key})
            error = out.get("error")
            if error is None:
                return out["value"]
            # A form may leave a name inert; a validator must not, or it
            # would flag valid configs.
            inert_ok = op == "schema"
            needed = out.get("export_needed")
            if needed:
                self._resolve_export(needed[0], needed[1], bindings, depth, inert_ok)
                continue
            missing = missing_reference(error)
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

    def _resolve_export(
        self,
        module_id: str,
        exported: str,
        bindings: dict[str, str],
        depth: int,
        inert_ok: bool,
    ) -> None:
        """Evaluate a pure export without starting its browser-only module."""
        source = self._module_body(module_id)
        ref = re.search(
            r"(?<=[{,])" + re.escape(exported) + r":(?:\(\)=>)?([\w$]+)(?=[,}])", source
        )
        definition = _local_definition(source, ref.group(1)) if ref else None
        if definition:
            self._evaluate(
                "export", source, definition, f"{module_id}:{exported}", depth + 1
            )
        elif inert_ok:
            # Form labels may depend on browser-only helpers; validators cannot.
            for name, value in list(bindings.items()):
                if value == module_id:
                    bindings[name] = "any"
        else:
            raise ValueError(f"unresolved export {module_id}:{exported}")
