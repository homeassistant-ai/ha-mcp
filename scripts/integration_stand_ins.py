#!/usr/bin/env python3
"""Refuse Core's test helpers registered under a real integration's name.

``mock_component``, ``MockModule`` (the module ``mock_integration`` registers),
``mock_platform``, ``mock_config_flow`` and ``hass.config.components.add`` give
a unit or app test a stand-in under the name it passes. Under a real Home
Assistant integration's name the test runs against that stand-in while the
integration's own code never runs (#3178). Core's own tests register mock
platforms under a made-up domain and set a real integration up with
``async_setup_component`` or a config entry.

The real integrations are the packages with a manifest in the installed
``homeassistant.components``. A name counts when it is a string literal, a
constant (also one imported from another module or a class attribute), a loop
or ``parametrize`` value (a dict's keys or values, ``enumerate`` and ``zip``
columns included), an f-string, concatenation, ``%``, ``str.format`` or
``str.join`` of those, or a value passed on through the parameters of
functions in the test trees, or of methods called on ``self`` or ``cls``,
until it reaches one of the helpers, also when a helper puts text around its
parameter. A name is read as it stood where it was bound: within one function
a later plain assignment replaces an earlier one, and a saved copy keeps what
it was given. ``zip`` stops at its shortest input when every length is known.
More than ``MAX_ALTERNATIVES`` possible names fail, since they are not checked,
and so does a name more than ``MAX_WRAPS`` helpers add text to, which is how a
helper calling itself looks. A name only known at runtime is left to review;
``--all`` lists where one reaches a helper, also through a function no call in
the trees reaches (a fixture, for example) or an argument passed with ``*`` or
``**``.

A kept case is an entry of ``ALLOWLIST`` naming the file, the integration and
the reason. An entry that matches nothing fails.
"""

from __future__ import annotations

import argparse
import ast
import functools
import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path

# The ratchet and the tests load this script by path, without its folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stand_in_allowlist import (  # type: ignore[import-not-found]
    ALLOWLIST,
    load_allowlist,
)
from stand_in_resolver import (  # type: ignore[import-not-found]
    COMPREHENSIONS,
    DEFINITIONS,
    ENDLESS,
    FUNCTIONS,
    MARKER,
    MAX_ALTERNATIVES,
    MAX_STEPS,
    MAX_WRAPS,
    NAME_HERE,
    SEQUENCES,
    TOO_MANY,
    UNKNOWN,
    Finding,
    Mode,
    Module,
    Param,
    Resolver,
    Site,
)
from stand_in_scopes import (  # type: ignore[import-not-found]
    AsOf,
    Bound,
    Chain,
    Imported,
    ModuleRef,
    Names,
)
from stand_in_syntax import (  # type: ignore[import-not-found]
    bound_argument,
    call_argument,
)

REPO = Path(__file__).resolve().parent.parent
# Each helper and the argument that names the integration: (position, keyword).
SINKS: dict[str, tuple[int, str | None]] = {
    "mock_component": (1, "component"),
    "MockModule": (0, "domain"),
    "mock_platform": (1, "platform_path"),
    "mock_config_flow": (0, "domain"),
    "config.components.add": (0, None),
}


def real_integrations() -> frozenset[str]:
    """The integrations of the installed Home Assistant."""
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("Home Assistant is not installed: run `uv sync --dev`")
    return frozenset(
        manifest.parent.name
        for location in spec.submodule_search_locations
        for manifest in Path(location, "components").glob("*/manifest.json")
    )


class Checker(Resolver):
    """The scan: every stand-in call in the test trees, and the helpers that
    pass names on to one."""

    # --- the scan ------------------------------------------------------------

    def run(self) -> list[Finding]:
        # Repeat until helpers passing names on to other helpers add no more;
        # each round only adds parameters, and MAX_WRAPS bounds the ones with
        # text around them, so this ends.
        while True:
            before = set(self.sinks)
            self.findings.clear()
            self.unresolved.clear()
            for module in self.local:
                self.visit(module, module.tree, [module.top])
            if self.sinks == before:
                self._uncalled()
                return sorted(
                    self.findings, key=lambda f: (f.path, f.line, f.integration)
                )

    def _uncalled(self) -> None:
        """A function no call in the trees reaches (pytest calls a test or a
        fixture) runs on its parameters' defaults; a parameter without one,
        and without ``parametrize`` values, holds a runtime name."""
        for sink, sites in self.sink_sites.items():
            if sink.function in self.called:
                continue
            default = self.defaults.get((sink.function, sink.name))
            if default is None:
                if (sink.function, sink.name) not in self.given:
                    self.unresolved |= sites
                continue
            scopes = default[1]

            def part(param: Param, scopes: Chain = scopes) -> Bound:
                found = self.defaults.get((param.function, param.name))
                if found is None:
                    return (ast.Constant(None), scopes)
                return self._part(found[0], found[1], param.mode)

            names = self._wrapped(sink, part(sink), part, scopes)
            for site in sites:
                self._judge(names, site, "", sink, follow=False)

    def visit(self, module: Module, node: ast.AST, chain: Chain) -> None:
        if isinstance(node, DEFINITIONS):
            self.visit_definition(module, node, chain)
            return
        if isinstance(node, COMPREHENSIONS):
            # Its targets are its own: nothing after it sees them.
            chain = [*chain, self.scope_of(module, node, chain)]
        if isinstance(node, ast.Call):
            self.check_call(module, node, chain)
        for child in ast.iter_child_nodes(node):
            self.visit(module, child, chain)

    def visit_definition(
        self,
        module: Module,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
        chain: Chain,
    ) -> None:
        if isinstance(node, ast.ClassDef):
            header: list[ast.AST] = [*node.decorator_list, *node.bases, *node.keywords]
        else:
            header = [*node.args.defaults, *filter(None, node.args.kw_defaults)]
            header += node.decorator_list if isinstance(node, FUNCTIONS) else []
        # Read where the definition stands: for a method, in its class's body.
        around = self.header_scopes(node, chain)
        for child in header:
            self.visit(module, child, around)
        inner = [*chain, self.scope_of(module, node, chain)]
        body = node.body if isinstance(node.body, list) else [node.body]
        for statement in body:
            # A method does not see the names its class body binds.
            method = isinstance(node, ast.ClassDef) and isinstance(
                statement, DEFINITIONS
            )
            self.visit(module, statement, chain if method else inner)

    def check_call(self, module: Module, call: ast.Call, chain: Chain) -> None:
        # What the callee's name holds where the call is made.
        here = AsOf(chain, (call.lineno, call.col_offset))
        name, core = self.callee_name(call, here)
        helper = name if core and name in SINKS else self._subclassed(call.func, here)
        if helper:
            position, keyword = SINKS[helper]
            self.record(module, call, call_argument(call, position, keyword), chain, "")
        # Resolve the callee when a helper passes names on: under its own name,
        # or under any name an import gives it (a re-export can rename it).
        if not self.sinks or (
            name not in {sink.function[1] for sink in self.sinks}
            and not self._imported(call.func, here)
        ):
            return
        for target in self._targets(call.func, here):
            key = self.functions.get(id(target))
            if key is None:
                continue
            self.called.add(key)
            bound = self.fills_first_parameter(call, target)
            for sink in tuple(self.sinks):
                if sink.function == key:
                    self.record(
                        module, call, None, chain, target.name, sink, (target, bound)
                    )

    def _imported(self, func: ast.expr, chain: Chain) -> bool:
        """Whether the callee's name, or the module it is read off, is imported."""
        while isinstance(func, ast.Attribute):
            func = func.value
        return isinstance(func, ast.Name) and any(
            isinstance(value, (Imported, ModuleRef))
            for value, _ in self.lookup(func.id, chain)
        )

    def _names(self, argument: ast.expr, chain: Chain, mode: Mode) -> set[str]:
        """The names ``argument`` gives, read as ``mode`` says."""
        if mode.kind == "value":
            direct: set[str] = self.values(argument, chain)
            return direct
        collect = self.items if mode.kind == "item" else self.dict_values
        names: set[str] = set().union(
            *(
                self.values(v, c or chain)
                for v, c in collect(argument, chain, frozenset(), mode.column)
            )
        )
        if not names and not isinstance(argument, (*SEQUENCES, ast.Dict)):
            names = {UNKNOWN}
        return names

    def fills_first_parameter(
        self, call: ast.Call, function: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> bool:
        """Whether ``call`` leaves a method's ``self`` or ``cls`` to Python."""
        if id(function) not in self.methods:
            return False
        decorators = {ast.unparse(d) for d in function.decorator_list}
        if "staticmethod" in decorators:
            return False
        func = call.func
        return "classmethod" in decorators or (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
        )

    def _targets(
        self, func: ast.expr, chain: Chain, depth: int = 0
    ) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        """The functions the callee can be, also through local aliases."""
        found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for target, own in self.objects(func, chain, frozenset()):
            if isinstance(target, FUNCTIONS):
                found.append(target)
            elif isinstance(target, ast.Name) and depth < 5:
                found += self._targets(target, own or chain, depth + 1)
        return found

    @functools.cached_property
    def _tree_definitions(self) -> set[int]:
        """The functions and classes the test trees define."""
        return {
            id(node)
            for module in self.local
            for node in ast.walk(module.tree)
            if isinstance(node, (*FUNCTIONS, ast.ClassDef))
        }

    def _in_trees(self, func: ast.expr, chain: Chain, depth: int = 0) -> bool:
        """Whether the callee is a function or class the test trees define."""
        for target, own in self.objects(func, chain, frozenset()):
            if id(target) in self._tree_definitions:
                return True
            if isinstance(target, ast.Name) and depth < 5:
                if self._in_trees(target, own or chain, depth + 1):
                    return True
        return False

    def _subclassed(self, func: ast.expr, chain: Chain, depth: int = 0) -> str | None:
        """The helper of Core's a class the trees define is built on, when the
        callee is such a class (``class Module(MockModule)``)."""
        for target, own in self.objects(func, chain, frozenset()):
            scopes = own or chain
            bases: list[ast.expr]
            if isinstance(target, ast.Name) and depth < 5:
                bases = [target]
            elif isinstance(target, ast.ClassDef) and depth < 5:
                bases = target.bases
            else:
                continue
            for base in bases:
                name, core = self.callee_name(ast.Call(base, [], []), scopes)
                if core and name in SINKS:
                    return name
                if found := self._subclassed(base, scopes, depth + 1):
                    return found
        return None

    def callee_name(self, call: ast.Call, chain: Chain) -> tuple[str | None, bool]:
        """The callee's name, read through imports and local aliases, and whether
        it can be one of Core's helpers: a function, method or class the trees
        define under a helper's name is not."""
        func = call.func
        core = not self._in_trees(func, chain)
        if isinstance(func, ast.Attribute):
            owner = ast.unparse(func.value).split(".")
            if func.attr == "add" and owner[-2:] == ["config", "components"]:
                return "config.components.add", core
            return func.attr, core
        if not isinstance(func, ast.Name):
            return None, core
        name, scopes = func.id, chain
        for _ in range(5):  # a local alias of a helper, a few names deep at most
            bound = self.lookup(name, scopes)
            imported = [value for value, _ in bound if isinstance(value, Imported)]
            if imported:
                helper: str = imported[0].name
                return helper, core
            aliases = [(v, own) for v, own in bound if isinstance(v, ast.Name)]
            if not aliases:
                break
            name, scopes = aliases[0][0].id, aliases[0][1] or scopes
        return name, core

    def record(
        self,
        module: Module,
        call: ast.Call,
        argument: ast.expr | None,
        chain: Chain,
        via: str,
        sink: Param | None = None,
        helper: tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool] | None = None,
    ) -> None:
        """Check the names ``call`` gives a stand-in: ``argument`` of a direct
        call, or what it passes for the parameter ``sink`` of ``helper`` (the
        function, and whether Python passes its first parameter)."""
        site = Site(module.path, call.lineno, ast.unparse(call.func))
        here = AsOf(chain, (call.lineno, call.col_offset))
        # ``*`` or ``**`` may carry an argument where it cannot be read.
        spread = any(isinstance(a, ast.Starred) for a in call.args) or any(
            keyword.arg is None for keyword in call.keywords
        )

        def part(param: Param) -> Bound | None:
            """What the call gives ``param``: its argument, or the parameter's
            default where the call leaves it out and no spread can fill it."""
            assert helper is not None
            given = bound_argument(call, helper[0], param.name, bound=helper[1])
            if given is not None:
                return self._part(given, here, param.mode)
            default = (
                None if spread else self.defaults.get((param.function, param.name))
            )
            return (
                None
                if default is None
                else self._part(default[0], default[1], param.mode)
            )

        names: set[str] | None = None
        if sink and helper:
            own = part(sink)
            if own is not None:
                unread: Bound = (ast.Constant(None), here)
                names = self._wrapped(
                    sink, own, lambda param: part(param) or unread, here
                )
        elif argument is not None:
            names = self.values(argument, here)
        if names is None:
            if spread:
                self.unresolved.add(site)
            return
        self._judge(names, site, via, sink)

    def _part(self, node: ast.expr, chain: Chain, mode: Mode) -> Bound:
        """``node`` as a part of a name: itself when it is the name, the names
        ``mode`` takes from it when it is a collection of them."""
        if mode.kind == "value":
            return (node, chain)
        return (Names(frozenset(self._names(node, chain, mode))), chain)

    def _judge(
        self,
        names: set[str],
        site: Site,
        via: str,
        sink: Param | None,
        *,
        follow: bool = True,
    ) -> None:
        """A finding for a real integration's name, a new sink for a helper's
        parameter (when ``follow``), a site left to review for a runtime name."""
        endless = Finding(site.path, site.line, ENDLESS, via)
        for name in names:
            domain = name.partition(".")[0]
            if (match := MARKER.search(domain)) and follow:
                self._follow(match, site, sink, endless)
            elif domain == TOO_MANY or domain in self.real:
                self.findings.add(Finding(site.path, site.line, domain, via))
            elif UNKNOWN in domain or match:
                self.unresolved.add(site)

    def _wrapped(
        self,
        sink: Param,
        own: Bound,
        part_of: Callable[[Param], Bound],
        chain: Chain,
    ) -> set[str]:
        """The names ``own`` gives ``sink``, inside the text its helper puts
        around them. The helper's other parameters in that text are filled in
        by ``part_of``: from the same call, and from the same loop row, so the
        parts of one name never come from two of either."""
        before, after = sink.mode.around or ("", "")
        parts: list[Bound] = []
        # ``split`` gives text, parameter number, text, ... in turn.
        for index, piece in enumerate(MARKER.split(f"{before}{NAME_HERE}{after}")):
            if index % 2:
                param = self.params[int(piece)]
                other = (ast.Constant(f"\x02{piece}\x03"), chain)
                parts.append(
                    part_of(param) if param.function == sink.function else other
                )
                continue
            text, name_here, rest = piece.partition(NAME_HERE)
            parts.append((ast.Constant(text), chain))
            if name_here:
                parts += [own, (ast.Constant(rest), chain)]
        joined: set[str] = self._join(parts, frozenset())
        return joined

    def _follow(
        self, match: re.Match[str], site: Site, sink: Param | None, endless: Finding
    ) -> None:
        """Make a sink of the helper parameter ``match`` found in a name, with
        the text the helpers put around it; that text may hold further
        parameters, followed the same way. ``endless`` is the finding when more
        than ``MAX_WRAPS`` helpers have added text."""
        domain = match.string
        around = (domain[: match.start()], domain[match.end() :])
        if UNKNOWN in "".join(around) or TOO_MANY in "".join(around):
            self.unresolved.add(site)
            return
        param = self.params[int(match.group(1))]
        if any(around):
            param = param._replace(mode=param.mode._replace(around=around))
            depth = self.depths.get(sink, 0) + 1 if sink else 1
            if param not in self.sinks and depth > MAX_WRAPS:
                self.findings.add(endless)
                return
            self.depths.setdefault(param, depth)
        self.sinks.add(param)
        self.sink_sites.setdefault(param, set()).add(site)


def _describe(finding: Finding) -> str:
    through = f" through {finding.via}()" if finding.via else ""
    if finding.integration == ENDLESS:
        return (
            f"{finding.path}:{finding.line}: more than {MAX_WRAPS} helpers add "
            f"text to the name{through}, as a helper calling itself does; name "
            "the integration directly"
        )
    if finding.integration == TOO_MANY:
        return (
            f"{finding.path}:{finding.line}: more than {MAX_ALTERNATIVES} possible "
            f"names to check, or a name more than {MAX_STEPS} names from its value"
            f"{through}; name the integration directly"
        )
    return (
        f"{finding.path}:{finding.line}: a stand-in registered under Home "
        f"Assistant's {finding.integration!r} integration{through}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument(
        "--all",
        action="store_true",
        help="list every case, allowlisted or not, and every name left to review",
    )
    args = parser.parse_args(argv)
    problems: list[str] = []
    allowed = load_allowlist(args.repo / ALLOWLIST, problems)
    # A list with a bad entry exempts nothing until it is fixed.
    exempt = {} if problems else allowed
    try:
        checker = Checker(args.repo, real_integrations())
        findings = checker.run()
    except SyntaxError as error:
        print(f"{error.filename}:{error.lineno}: {error.msg}")
        return 1
    if args.all:
        listed = [_describe(f) for f in findings] + [
            f"{path}:{line}: {callee}() gets a name only known at runtime"
            for path, line, callee in sorted(checker.unresolved)
        ]
        if listed:
            print("\n".join(listed))
    problems += [
        _describe(f) for f in findings if (f.path, f.integration) not in exempt
    ]
    matched = {(f.path, f.integration) for f in findings}
    problems += [
        f"{ALLOWLIST}: {path} no longer registers {name!r}; remove the entry"
        for path, name in sorted(set(allowed) - matched)
    ]
    if problems:
        print("\n".join(problems))
        print(
            "Set the real integration up as Core's own tests do, or register the "
            "stand-in under a made-up domain (tests/AGENTS.md)."
        )
        return 1
    print(f"No unlisted stand-in for a real integration; {len(allowed)} allowlisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
