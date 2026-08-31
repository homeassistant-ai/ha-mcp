"""Tests for parameter type/description extraction in scripts/extract_tools.py.

``_extract_field_info`` reads the ``Annotated[type, Field(description=...)]``
form that every tool signature uses. Its Annotated branch used to match only
the dotted ``typing.Annotated`` spelling, which no tool file uses, so every
parameter fell through to the raw-``ast.unparse`` fallback: descriptions were
dropped and the whole annotation source landed in ``type``.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# The generator is a script, not a package module — same import route the
# other extractor tests use.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import extract_tools  # noqa: E402


def _field_info(annotation_source: str) -> dict:
    """Run _extract_field_info over a single annotation expression."""
    return extract_tools._extract_field_info(ast.parse(annotation_source).body[0].value)


def _has_description(schema: dict) -> bool:
    """Whether a parameter carries description text a reader can use.

    Truthiness alone would accept ``"   "`` — text the site renders as a blank
    cell, which is the failure these assertions exist to catch.
    """
    description = schema.get("description")
    return isinstance(description, str) and bool(description.strip())


@pytest.fixture(autouse=True)
def _clear_module_scope_caches():
    """The resolver's caches are module globals; keep tests order-independent."""
    extract_tools._SCOPE_CACHE.clear()
    extract_tools._RESOLVING.clear()
    extract_tools.UNRESOLVED_KEYWORDS.clear()
    yield
    extract_tools._SCOPE_CACHE.clear()
    extract_tools._RESOLVING.clear()
    extract_tools.UNRESOLVED_KEYWORDS.clear()


def _definitions(source: str) -> extract_tools.ModuleScope:
    """Module-level constants and helpers resolved from ``source``."""
    scope = extract_tools.ModuleScope({}, {})
    extract_tools._apply_definitions(ast.parse(source), scope)
    return scope


def _tool_params(signature: str) -> tuple[dict, list[str]]:
    """Run the parameter extractor over one tool signature."""
    node = ast.parse(f"async def tool(self, {signature}) -> None:\n    pass\n").body[0]
    return extract_tools._extract_tool_params(node)


def _governing_annotation(annotation: ast.expr | None) -> ast.expr | None:
    """The ``Annotated`` node whose metadata governs the whole parameter.

    Spelled out here rather than borrowed from the extractor, so these
    invariants stay an independent statement of the contract: metadata governs
    the parameter when the annotation is ``Annotated[...]``, or an optional
    whose every other branch is ``None``. Anywhere else — a second
    metadata-bearing branch, or a plain sibling type — the extractor
    deliberately publishes nothing, and demanding it here would fail a correct
    extraction.
    """
    if annotation is None:
        return None
    if isinstance(annotation, ast.Subscript) and extract_tools._is_annotated(
        annotation.value
    ):
        return annotation
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        operands: list[ast.expr] = []

        def _flatten(node: ast.expr) -> None:
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                _flatten(node.left)
                _flatten(node.right)
            else:
                operands.append(node)

        _flatten(annotation)
        annotated = [
            o
            for o in operands
            if isinstance(o, ast.Subscript) and extract_tools._is_annotated(o.value)
        ]
        others = [o for o in operands if o not in annotated]
        if len(annotated) == 1 and all(ast.unparse(o) == "None" for o in others):
            return annotated[0]
    return None


def _constraints_declared_in_source() -> dict[tuple[str, str], set[str]]:
    """Resolvable non-description Field kwargs, per (tool name, parameter).

    ``validation_alias=AliasChoices(...)`` is deliberately not resolved — the
    three parameters using it spell the shorthand out in their own prose — so
    only kwargs with a literal value are demanded here.
    """
    declared: dict[tuple[str, str], set[str]] = {}
    for path in extract_tools.TOOL_FILES:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            _, tool_name = extract_tools._find_tool_decorator(node)
            if tool_name is None:
                continue
            for arg in node.args.args:
                if arg.annotation is None:
                    continue
                governing = _governing_annotation(arg.annotation)
                if governing is None:
                    continue
                kwargs = {
                    kw.arg
                    for sub in ast.walk(governing)
                    if extract_tools._is_field_call(sub)
                    for kw in sub.keywords
                    if kw.arg not in (None, "description", "default")
                    and isinstance(kw.value, (ast.Constant, ast.UnaryOp))
                }
                if kwargs:
                    declared[(tool_name, arg.arg)] = kwargs
    return declared


def _params_documented_in_source() -> set[tuple[str, str]]:
    """(tool name, parameter) pairs whose annotation carries a Field description."""
    documented: set[tuple[str, str]] = set()
    for path in extract_tools.TOOL_FILES:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            _, tool_name = extract_tools._find_tool_decorator(node)
            if tool_name is None:
                continue
            for arg in node.args.args:
                governing = _governing_annotation(arg.annotation)
                if governing is None:
                    continue
                if any(
                    kw.arg == "description"
                    for sub in ast.walk(governing)
                    if extract_tools._is_field_call(sub)
                    for kw in sub.keywords
                ):
                    documented.add((tool_name, arg.arg))
    return documented


class TestExtractFieldInfo:
    """Annotated metadata is extracted for every spelling tool files use."""

    def test_bare_annotated_extracts_type_and_description(self):
        """``Annotated`` imported bare (the spelling every tool file uses)."""
        info = _field_info(
            "Annotated[str | None, Field(description='Service domain.')]"
        )

        assert info["type"] == "str | None"
        assert info["description"] == "Service domain."

    def test_dotted_annotated_extracts_type_and_description(self):
        """``typing.Annotated`` keeps working."""
        info = _field_info(
            "typing.Annotated[bool, Field(description='Verbose output.')]"
        )

        assert info["type"] == "bool"
        assert info["description"] == "Verbose output."

    def test_validator_between_type_and_field_is_skipped(self):
        """A bare validator (e.g. JSON_STRING_COERCION) sits between the two."""
        info = _field_info(
            "Annotated[dict[str, Any] | None, JSON_STRING_COERCION, "
            "Field(description='Extra params.')]"
        )

        assert info["type"] == "dict[str, Any] | None"
        assert info["description"] == "Extra params."

    def test_annotated_nested_in_a_union_rejoins_the_type(self):
        """``Annotated[int, Field(ge=1)] | None`` — the union is the outer node."""
        info = _field_info("Annotated[int, Field(ge=1)] | None")

        assert info["type"] == "int | None"

    def test_union_keeps_the_description_from_its_annotated_side(self):
        info = _field_info("Annotated[int, Field(description='Page size.')] | None")

        assert info["type"] == "int | None"
        assert info["description"] == "Page size."

    def test_field_default_is_extracted(self):
        info = _field_info(
            "Annotated[str | None, Field(default=None, description='x')]"
        )

        assert info["default"] is None

    def test_plain_annotation_passes_through(self):
        assert _field_info("str | None") == {"type": "str | None"}

    def test_no_annotation(self):
        assert extract_tools._extract_field_info(None) == {}


class TestFieldConstraints:
    """``Field`` bounds survive the cleanup of ``type``."""

    def test_numeric_bounds_are_recorded(self):
        info = _field_info("Annotated[int, Field(ge=1, le=60)]")

        assert info["constraints"] == {"ge": 1, "le": 60}

    def test_exclusive_and_length_bounds(self):
        info = _field_info(
            "Annotated[list[str], Field(gt=0, lt=5, min_length=1, max_length=3)]"
        )

        assert info["constraints"] == {
            "gt": 0,
            "lt": 5,
            "min_length": 1,
            "max_length": 3,
        }

    def test_length_bounds_are_not_translated_to_json_schema(self):
        """``min_length`` is ``minLength`` on a str but ``minItems`` on a list.

        The annotation is a Python type, so nothing here can tell the two
        apart — translating would mislabel one of them.
        """
        info = _field_info("Annotated[list[str] | None, Field(min_length=1)]")

        assert set(info["constraints"]) == {"min_length"}

    def test_non_bound_validation_keywords_are_kept_too(self):
        """``strict`` / ``allow_inf_nan`` are validation rules, not decoration.

        They were visible in the annotation source this extractor used to dump
        into ``type``; a whitelist of bounds would drop them silently.
        """
        info = _field_info(
            "Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]"
        )

        assert info["constraints"] == {
            "ge": 0,
            "strict": True,
            "allow_inf_nan": False,
        }

    def test_negative_bounds_survive(self):
        """``ge=-1`` parses as a unary minus, not a plain literal."""
        info = _field_info("Annotated[int, Field(ge=-1, le=-0.5)]")

        assert info["constraints"] == {"ge": -1, "le": -0.5}

    def test_bounds_survive_a_union(self):
        info = _field_info("Annotated[int, Field(ge=1)] | None")

        assert info["type"] == "int | None"
        assert info["constraints"] == {"ge": 1}

    def test_no_constraints_key_without_bounds(self):
        assert "constraints" not in _field_info(
            "Annotated[str, Field(description='x')]"
        )


class TestUnionMetadata:
    """Metadata belongs to the branch that declared it."""

    def test_metadata_from_the_single_annotated_branch_applies(self):
        info = _field_info("Annotated[int, Field(ge=1, description='n')] | None")

        assert info["type"] == "int | None"
        assert info["constraints"] == {"ge": 1}
        assert info["description"] == "n"

    def test_metadata_is_not_published_across_a_non_optional_branch(self):
        """``ge=1`` says nothing about the ``str`` a caller may pass instead."""
        info = _field_info("Annotated[int, Field(ge=1)] | str")

        assert info["type"] == "int | str"
        assert "constraints" not in info

    def test_a_three_way_union_publishes_no_metadata(self):
        """``ge`` on the int branch says nothing about the str a caller may pass."""
        info = _field_info("Annotated[int, Field(ge=1, description='n')] | str | None")

        assert info["type"] == "int | str | None"
        assert "constraints" not in info
        assert "description" not in info

    def test_two_annotated_branches_publish_no_flattened_metadata(self):
        """Neither branch's rules govern the whole parameter."""
        info = _field_info(
            "Annotated[int, Field(ge=1)] | Annotated[str, Field(min_length=1)]"
        )

        assert info["type"] == "int | str"
        assert "constraints" not in info

    def test_same_named_constraints_are_not_silently_overwritten(self):
        info = _field_info(
            "Annotated[int, Field(ge=1)] | Annotated[float, Field(ge=99)]"
        )

        assert "constraints" not in info


class TestZeroArgumentHelpers:
    """Only a helper that can actually be called bare is resolved."""

    def _funcs(self, source: str) -> dict:
        return _definitions(source).funcs

    def test_a_truly_zero_argument_helper_resolves(self):
        assert self._funcs("def docs():\n    return 'text'\n") == {"docs": "text"}

    def test_a_conditional_return_disqualifies_the_helper(self):
        """Two return paths mean no single value; the last one is not it."""
        source = "def docs():\n    if enabled:\n        return 'A'\n    return 'B'\n"

        assert self._funcs(source) == {}

    def test_a_single_conditional_return_disqualifies_the_helper(self):
        """Falling past the ``if`` returns None, so the string is not its value."""
        source = "def docs():\n    if enabled:\n        return 'A'\n"

        assert self._funcs(source) == {}

    def test_a_keyword_only_parameter_disqualifies_the_helper(self):
        """``docs()`` would raise TypeError, so its return value is not its value."""
        assert self._funcs("def docs(*, mode):\n    return 'text'\n") == {}

    def test_a_positional_only_parameter_disqualifies_the_helper(self):
        assert self._funcs("def docs(mode, /):\n    return 'text'\n") == {}

    def test_varargs_disqualify_the_helper(self):
        assert self._funcs("def docs(*a, **k):\n    return 'text'\n") == {}


class TestMetadataSelection:
    """Only ``Field`` keywords describe the parameter."""

    def test_other_metadata_calls_contribute_nothing(self):
        """Their keywords are their own, not pydantic validation rules."""
        info = _field_info("Annotated[int, OtherMetadata(mode='strict')]")

        assert info == {"type": "int"}

    def test_field_is_still_read_beside_other_metadata(self):
        info = _field_info("Annotated[int, OtherMetadata(mode='strict'), Field(ge=1)]")

        assert info["constraints"] == {"ge": 1}


class TestRequiredParameters:
    """``required`` must agree with the defaults the schema reports."""

    def test_field_default_alone_makes_a_parameter_optional(self):
        """FastMCP treats ``Field(default=...)`` as the default; so must this."""
        properties, required = _tool_params("limit: Annotated[int, Field(default=10)]")

        assert properties["limit"]["default"] == 10
        assert required == []

    def test_a_parameter_with_no_default_at_all_stays_required(self):
        properties, required = _tool_params("entity_id: str")

        assert required == ["entity_id"]
        assert "default" not in properties["entity_id"]

    def test_an_unresolvable_field_default_still_means_optional(self):
        """Requiredness comes from the source, not from what resolved.

        Calling a parameter required because its default could not be read
        states the opposite of the truth to whoever reads the page.
        """
        properties, required = _tool_params(
            "limit: Annotated[int, Field(description='n', default=SOME_UNKNOWN)]"
        )

        assert required == []
        assert "default" not in properties["limit"]

    def test_a_signature_default_that_is_a_module_constant_resolves(self):
        """The resolver is already in hand; a named default is not a mystery."""
        scope = extract_tools.ModuleScope({"DEFAULT_WIDTH": 1280}, {})
        node = ast.parse(
            "async def tool(self, width: int = DEFAULT_WIDTH) -> None:\n    pass\n"
        ).body[0]
        properties, required = extract_tools._extract_tool_params(node, scope)

        assert properties["width"]["default"] == 1280
        assert required == []

    def test_a_positional_field_default_is_a_default(self):
        """``Field(5)`` and ``Field(default=5)`` mean the same to pydantic."""
        properties, required = _tool_params(
            "width: Annotated[int, Field(5, description='W')]"
        )

        assert properties["width"]["default"] == 5
        assert required == []

    def test_field_ellipsis_is_pydantic_s_required_marker_not_a_default(self):
        """Both spellings: ``Field(...)`` and ``Field(default=...)``."""
        for annotation in (
            "Annotated[int, Field(..., description='W')]",
            "Annotated[int, Field(default=..., description='W')]",
        ):
            properties, required = _tool_params(f"width: {annotation}")

            assert required == ["width"], annotation
            assert "default" not in properties["width"], annotation

    def test_default_factory_makes_a_parameter_optional(self):
        """pydantic calls the factory when the argument is omitted.

        The callable itself is not published — it is how the value is made,
        not a value, and a function is not JSON.
        """
        properties, required = _tool_params(
            "tags: Annotated[list[str], Field(default_factory=list, description='T')]"
        )

        assert required == []
        assert "default" not in properties["tags"]
        assert extract_tools.UNRESOLVED_KEYWORDS == []

    def test_every_required_name_has_a_property_to_point_at(self):
        """A required name with no property is a schema nothing can render."""
        properties, required = _tool_params("foo")

        assert set(required) <= set(properties)


class TestUnresolvedValuesAreReported:
    """A value that cannot be read is announced, never quietly dropped."""

    def test_an_unresolvable_constraint_is_recorded(self):
        """The whitelist was removed so nothing goes missing by omission."""
        _field_info("Annotated[int, Field(ge=CONF_MAX)]")

        assert any(
            "ge=CONF_MAX" in entry for entry in extract_tools.UNRESOLVED_KEYWORDS
        )

    def test_an_unresolvable_positional_default_is_recorded(self):
        _field_info("Annotated[int, Field(NOT_KNOWN)]")

        assert any("NOT_KNOWN" in entry for entry in extract_tools.UNRESOLVED_KEYWORDS)

    def test_a_deliberately_unresolved_callable_is_not_reported(self):
        """A standing entry every run is what trains a reader to skip the new one."""
        _field_info("Annotated[int, Field(validation_alias=AliasChoices('a', 'b'))]")
        _field_info("Annotated[int, Field(default=cast(Any, None))]")

        assert extract_tools.UNRESOLVED_KEYWORDS == []

    def test_a_resolvable_constraint_is_not_recorded(self):
        _field_info("Annotated[int, Field(ge=1)]")

        assert extract_tools.UNRESOLVED_KEYWORDS == []


class TestMalformedAnnotations:
    """A wrong answer is worse than handing back the source text."""

    def test_a_union_operand_without_a_type_falls_back_to_source(self):
        """Joining the rest would name one branch as the whole type."""
        info = _field_info("Annotated[int] | None")

        assert info["type"] == "Annotated[int] | None"

    def test_an_unknown_fstring_conversion_yields_no_description(self):
        """An unrecognised conversion code must not abort the catalog build."""
        node = ast.parse("Annotated[str, Field(description=f'{X}')]").body[0].value
        node.slice.elts[1].keywords[0].value.values[0].conversion = 42

        info = extract_tools._extract_field_info(
            node, extract_tools.ModuleScope({"X": "v"}, {})
        )

        assert "description" not in info


class TestNonLiteralDescriptions:
    """Descriptions built from constants and helpers still reach the catalog."""

    def _scope(self, consts=None, funcs=None):
        return extract_tools.ModuleScope(consts or {}, funcs or {})

    def _resolve(self, source: str, scope) -> dict:
        return extract_tools._extract_field_info(ast.parse(source).body[0].value, scope)

    def test_module_constant_reference(self):
        scope = self._scope(consts={"_DESC": "Initial value."})
        info = self._resolve("Annotated[str, Field(description=_DESC)]", scope)

        assert info["description"] == "Initial value."

    def test_fstring_interpolating_constants(self):
        scope = self._scope(consts={"MAX_LIMIT": 500})
        info = self._resolve(
            "Annotated[int, Field(description=f'Capped at {MAX_LIMIT}.')]", scope
        )

        assert info["description"] == "Capped at 500."

    def test_fstring_conversions_are_applied(self):
        """``!r`` quotes the value; ignoring it yields a wrong description."""
        scope = self._scope(consts={"SEP": ","})
        info = self._resolve(
            "Annotated[str, Field(description=f'Split on {SEP!r}.')]", scope
        )

        assert info["description"] == "Split on ','."

    def test_sliced_and_method_called_constant(self):
        scope = self._scope(consts={"DESC": "capture the whole page"})
        info = self._resolve(
            "Annotated[bool, Field(description=f'{DESC[:1].upper()}{DESC[1:]}.')]",
            scope,
        )

        assert info["description"] == "Capture the whole page."

    def test_concatenation_with_a_zero_arg_helper(self):
        scope = self._scope(funcs={"get_security_documentation": " SECURITY: none."})
        info = self._resolve(
            "Annotated[str, Field(description='Transform. '"
            " + get_security_documentation())]",
            scope,
        )

        assert info["description"] == "Transform.  SECURITY: none."

    def test_a_constant_that_is_none_is_a_value_not_a_failure(self):
        """``DEFAULT = None`` must reach the schema as a real default."""
        scope = self._scope(consts={"DEFAULT": None})
        info = self._resolve("Annotated[str | None, Field(default=DEFAULT)]", scope)

        assert "default" in info
        assert info["default"] is None

    def test_unresolvable_description_is_omitted_not_truncated(self):
        """A partial resolution would silently drop half the documentation."""
        info = self._resolve(
            "Annotated[str, Field(description='Transform. ' + unknown_helper())]",
            self._scope(),
        )

        assert "description" not in info


class TestForwardReferences:
    """Definition order in the source must not decide what resolves."""

    def test_helper_returning_a_later_constant_resolves(self):
        """A single source-order pass would leave this helper unresolved."""
        scope = _definitions(
            "def docs():\n    return TEXT\n\n\nTEXT = 'Shared documentation.'\n"
        )

        assert scope.funcs["docs"] == "Shared documentation."

    def test_constant_built_from_a_later_constant_resolves(self):
        scope = _definitions("HEAD = LEAD + ' tail.'\n\nLEAD = 'Lead'\n")

        assert scope.consts["HEAD"] == "Lead tail."

    def test_a_genuinely_unresolvable_name_stays_out(self):
        scope = _definitions("VALUE = missing_helper()\n")

        assert "VALUE" not in scope.consts

    def test_a_module_level_assignment_overrides_an_imported_name(self):
        """Python's own precedence: the local binding is what the module uses."""
        scope = extract_tools.ModuleScope({"NAME": "imported"}, {})
        extract_tools._apply_definitions(
            ast.parse("from .sibling import NAME\nNAME = 'local'\n"), scope
        )

        assert scope.consts["NAME"] == "local"


class TestResolverRejections:
    """The evaluator's boundaries — what it declines to read, and why."""

    def test_a_format_spec_is_not_resolved(self):
        """Formatting is evaluation; the catalog reads, it does not compute."""
        scope = extract_tools.ModuleScope({"MAX": 500}, {})
        info = extract_tools._extract_field_info(
            ast.parse("Annotated[int, Field(description=f'{MAX:g}')]").body[0].value,
            scope,
        )

        assert "description" not in info

    def test_a_non_slice_subscript_is_not_resolved(self):
        scope = extract_tools.ModuleScope({"DESC": "text"}, {})
        info = extract_tools._extract_field_info(
            ast.parse("Annotated[str, Field(description=DESC[0])]").body[0].value,
            scope,
        )

        assert "description" not in info

    def test_a_helper_call_with_arguments_is_not_resolved(self):
        scope = extract_tools.ModuleScope({}, {"docs": "text"})
        info = extract_tools._extract_field_info(
            ast.parse("Annotated[str, Field(description=docs('x'))]").body[0].value,
            scope,
        )

        assert "description" not in info

    def test_only_allowlisted_string_methods_run(self):
        """This allowlist is what backs "never executes repository source"."""
        scope = extract_tools.ModuleScope({"DESC": "a b"}, {})
        info = extract_tools._extract_field_info(
            ast.parse("Annotated[str, Field(description=DESC.split())]").body[0].value,
            scope,
        )

        assert "description" not in info

    def test_a_field_kwargs_splat_contributes_nothing(self):
        info = _field_info("Annotated[int, Field(**COMMON)]")

        assert info == {"type": "int"}


class TestImportResolution:
    """Every import spelling the package uses, and the containment guard."""

    def _source(self, statement: str):
        node = ast.parse(statement).body[0]
        return extract_tools._import_source(
            extract_tools.TOOLS_DIR / "tools_logs.py", node
        )

    def test_relative_module_import(self):
        assert self._source("from .log_common import MAX_LIMIT") == (
            extract_tools.TOOLS_DIR / "log_common.py"
        )

    def test_package_initializer_import(self):
        assert self._source("from . import NAME") == (
            extract_tools.TOOLS_DIR / "__init__.py"
        )

    def test_parent_package_import(self):
        assert self._source("from ..utils.python_sandbox import x") == (
            extract_tools.PACKAGE_ROOT / "utils" / "python_sandbox.py"
        )

    def test_absolute_intra_package_import(self):
        """The package writes both spellings; both name the same file."""
        assert self._source("from ha_mcp.utils.python_sandbox import x") == (
            extract_tools.PACKAGE_ROOT / "utils" / "python_sandbox.py"
        )

    def test_an_import_from_outside_the_package_is_declined(self):
        """The containment guard is what keeps the extractor out of the world."""
        assert self._source("from os import path") is None
        assert self._source("from pydantic import Field") is None

    def test_an_aliased_name_is_bound_under_its_local_spelling(self):
        scope = extract_tools.ModuleScope({}, {})
        extract_tools._apply_imports(
            extract_tools.TOOLS_DIR / "tools_logs.py",
            ast.parse("from .log_common import MAX_LIMIT as CAP"),
            scope,
        )

        source_scope = extract_tools._module_scope(
            extract_tools.TOOLS_DIR / "log_common.py"
        )

        assert scope.consts["CAP"] == source_scope.consts["MAX_LIMIT"]
        assert "MAX_LIMIT" not in scope.consts


class TestImportCycles:
    """A scope built through a cycle is incomplete and must not be cached."""

    def _package(self, tmp_path, files: dict[str, str]):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        for name, source in files.items():
            (pkg / name).write_text(source, encoding="utf-8")
        return pkg

    def test_no_module_in_a_cycle_is_cached(self, tmp_path, monkeypatch):
        """Including the ones that only imported THROUGH the cut.

        A → B → C → A: C sees the cycle, but B is built from C's partial
        answer, so caching B would serve an incomplete module for the rest of
        the run — and which module lost names would depend on file order.
        """
        pkg = self._package(
            tmp_path,
            {
                "a.py": "from .b import B_NAME\nA_NAME = 'a'\n",
                "b.py": "from .c import C_NAME\nB_NAME = 'b'\n",
                "c.py": "from .a import A_NAME\nC_NAME = 'c'\n",
            },
        )
        monkeypatch.setattr(extract_tools, "PACKAGE_ROOT", pkg)

        extract_tools._module_scope(pkg / "a.py")

        assert not any(
            pkg / name in extract_tools._SCOPE_CACHE
            for name in ("a.py", "b.py", "c.py")
        )

    def test_an_acyclic_import_chain_is_cached(self, tmp_path, monkeypatch):
        """The cache is what keeps the walk from re-expanding per tool file."""
        pkg = self._package(
            tmp_path,
            {
                "a.py": "from .b import B_NAME\n",
                "b.py": "B_NAME = 'b'\n",
            },
        )
        monkeypatch.setattr(extract_tools, "PACKAGE_ROOT", pkg)

        scope = extract_tools._module_scope(pkg / "a.py")

        assert scope.consts["B_NAME"] == "b"
        assert pkg / "b.py" in extract_tools._SCOPE_CACHE

    def test_an_unreadable_module_degrades_instead_of_crashing(
        self, tmp_path, monkeypatch, capsys
    ):
        """One bad file must not abort the catalog — but it must say so."""
        pkg = self._package(tmp_path, {"broken.py": "def (\n"})
        monkeypatch.setattr(extract_tools, "PACKAGE_ROOT", pkg)

        scope = extract_tools._module_scope(pkg / "broken.py")

        assert scope.consts == {}
        assert "cannot read module scope" in capsys.readouterr().err


class TestModuleScope:
    """Scope building reads constants across the package's own imports."""

    @pytest.fixture(scope="class")
    def logs_scope(self):
        return extract_tools._module_scope(
            REPO_ROOT / "src" / "ha_mcp" / "tools" / "tools_logs.py"
        )

    def test_constants_are_pulled_through_relative_imports(self, logs_scope):
        assert isinstance(logs_scope.consts.get("MAX_LIMIT"), int)

    def test_zero_arg_helpers_resolve_across_modules(self):
        scope = extract_tools._module_scope(
            REPO_ROOT / "src" / "ha_mcp" / "tools" / "tools_config_scenes.py"
        )

        resolved = scope.funcs["get_security_documentation"]

        assert isinstance(resolved, str) and resolved.strip()


class TestExtractedToolsNeverLeakAnnotationSource:
    """The generated site data must carry real types, not annotation source."""

    @pytest.fixture(scope="class")
    def tools(self) -> list[dict]:
        return extract_tools.extract_tools()

    def test_no_parameter_type_is_raw_annotated_source(self, tools):
        leaked = [
            f"{tool['name']}.{param}: {schema['type']}"
            for tool in tools
            for param, schema in tool["inputSchema"].get("properties", {}).items()
            if str(schema.get("type", "")).startswith("Annotated[")
        ]

        assert not leaked, (
            f"{len(leaked)} parameter(s) have unparsed Annotated source as their "
            "type — _extract_field_info did not recognise the annotation:\n"
            + "\n".join(f"  - {entry}" for entry in leaked[:10])
        )

    def test_field_descriptions_reach_the_extracted_schema(self, tools):
        """Tools that document parameters must surface those descriptions."""
        described = [
            param
            for tool in tools
            for param, schema in tool["inputSchema"].get("properties", {}).items()
            if _has_description(schema)
        ]

        assert described, (
            "No extracted parameter carries a description, but tool signatures "
            "use Field(description=...) throughout — extraction is broken."
        )

    def test_every_declared_constraint_reaches_the_schema(self, tools):
        """Constraints get the same source-derived guard descriptions have."""
        extracted = {
            (tool["name"], param): schema.get("constraints", {})
            for tool in tools
            for param, schema in tool["inputSchema"].get("properties", {}).items()
        }
        missing = [
            f"{name}.{param}: {kwarg}"
            for (name, param), kwargs in _constraints_declared_in_source().items()
            for kwarg in kwargs
            if kwarg not in extracted.get((name, param), {})
        ]

        assert not missing, (
            "Field constraint(s) declared in a tool signature that never reach "
            "the catalog:\n" + "\n".join(f"  - {entry}" for entry in missing)
        )

    def test_every_documented_parameter_resolves_its_description(self, tools):
        """A parameter whose source documents it must not lose that text.

        Descriptions built from constants, f-strings or helper calls resolve
        statically; anything this misses reaches the site as a blank cell.
        """
        extracted = {
            (tool["name"], param): schema
            for tool in tools
            for param, schema in tool["inputSchema"].get("properties", {}).items()
        }
        undocumented = [
            f"{name}.{param}"
            for name, param in _params_documented_in_source()
            if not _has_description(extracted.get((name, param), {}))
        ]

        assert not undocumented, (
            "Parameters whose signature carries Field(description=...) but whose "
            "text did not resolve (a parameter missing from the catalog "
            "entirely counts — the site cannot show that loss at all):\n"
            + "\n".join(f"  - {entry}" for entry in undocumented)
        )
