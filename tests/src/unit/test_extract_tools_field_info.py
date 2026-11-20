"""Tests for parameter type/description extraction in scripts/extract_tools.py.

The extractor reads the ``Annotated[type, Field(description=...)]`` form that
every tool signature uses. Its Annotated branch used to match only the dotted
``typing.Annotated`` spelling, which no tool file uses, so every parameter fell
through to the raw-``ast.unparse`` fallback: descriptions were dropped and the
whole annotation source landed in ``type``.

Each test writes tool files into a scratch ``ha_mcp`` package, runs
``extract_tools()`` over them and reads the catalog entry the site renders.
"""

import ast
import importlib.util
from pathlib import Path
from typing import Any, TypeGuard

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# The generator is a script, not a package module: load it by path.
_SPEC = importlib.util.spec_from_file_location(
    "extract_tools", REPO_ROOT / "scripts" / "extract_tools.py"
)
assert _SPEC is not None and _SPEC.loader is not None
extract_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(extract_tools)


def _has_description(schema: dict) -> bool:
    """Whether a parameter carries description text a reader can use.

    Truthiness alone would accept ``"   "`` — text the site renders as a blank
    cell, which is the failure these assertions exist to catch.
    """
    description = schema.get("description")
    return isinstance(description, str) and bool(description.strip())


@pytest.fixture(autouse=True)
def _fresh_unresolved_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's unresolved-value report is a module global; start each empty."""
    monkeypatch.setattr(extract_tools, "UNRESOLVED_KEYWORDS", [])


class Catalog:
    """Runs the extractor over tool files written into a scratch package."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self._monkeypatch = monkeypatch
        self._written = 0

    def module(self, relative: str, source: str) -> None:
        """Write a non-tool module the tool files can import from."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def tools(self, *sources: str) -> dict[str, dict[str, Any]]:
        """Extract the catalog of tool files holding ``sources``, in order.

        Each file gets a name of its own: a module's names are cached by path
        for the run, so reusing one would read an earlier test step's scope.
        """
        files = []
        for source in sources:
            self._written += 1
            path = self.root / "tools" / f"tools_sample_{self._written}.py"
            path.write_text(source, encoding="utf-8")
            files.append(path)
        self._monkeypatch.setattr(extract_tools, "TOOL_FILES", files)
        return {tool["name"]: tool for tool in extract_tools.extract_tools()}

    def schema(self, signature: str, prelude: str = "") -> dict[str, Any]:
        """The input schema of one tool with ``signature``."""
        source = (
            f"{prelude}\n\n\n@mcp.tool()\nasync def ha_sample({signature}) -> None:\n"
            "    pass\n"
        )
        schema: dict[str, Any] = self.tools(source)["ha_sample"]["inputSchema"]
        return schema

    def param(self, annotation: str, prelude: str = "") -> dict[str, Any]:
        """The catalog entry of one parameter annotated ``annotation``."""
        entry: dict[str, Any] = self.schema(f"p: {annotation}", prelude)["properties"][
            "p"
        ]
        return entry


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    root = tmp_path / "ha_mcp"
    (root / "tools").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(extract_tools, "PACKAGE_ROOT", root)
    return Catalog(root, monkeypatch)


def _names_annotated(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Annotated") or (
        isinstance(node, ast.Attribute) and node.attr == "Annotated"
    )


def _is_field(node: ast.AST) -> TypeGuard[ast.Call]:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "Field")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "Field")
    )


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
    if isinstance(annotation, ast.Subscript) and _names_annotated(annotation.value):
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
            if isinstance(o, ast.Subscript) and _names_annotated(o.value)
        ]
        others = [o for o in operands if o not in annotated]
        if len(annotated) == 1 and all(ast.unparse(o) == "None" for o in others):
            return annotated[0]
    return None


def _declared_tool_name(node: ast.AsyncFunctionDef) -> str | None:
    """The ``ha_*`` name a ``@mcp.tool(...)`` / ``@tool(...)`` decorator gives
    a function: its ``name=`` when it passes one, else the function's own."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not (
            (isinstance(func, ast.Attribute) and func.attr == "tool")
            or (isinstance(func, ast.Name) and func.id == "tool")
        ):
            continue
        name = node.name
        for keyword in decorator.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                name = str(keyword.value.value)
        return name if name.startswith("ha_") else None
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
            tool_name = _declared_tool_name(node)
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
                    if _is_field(sub)
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
            tool_name = _declared_tool_name(node)
            if tool_name is None:
                continue
            for arg in node.args.args:
                governing = _governing_annotation(arg.annotation)
                if governing is None:
                    continue
                if any(
                    kw.arg == "description"
                    for sub in ast.walk(governing)
                    if _is_field(sub)
                    for kw in sub.keywords
                ):
                    documented.add((tool_name, arg.arg))
    return documented


class TestExtractFieldInfo:
    """Annotated metadata is extracted for every spelling tool files use."""

    def test_bare_annotated_extracts_type_and_description(self, catalog: Catalog):
        """``Annotated`` imported bare (the spelling every tool file uses)."""
        info = catalog.param(
            "Annotated[str | None, Field(description='Service domain.')]"
        )

        assert info["type"] == "str | None"
        assert info["description"] == "Service domain."

    def test_dotted_annotated_extracts_type_and_description(self, catalog: Catalog):
        """``typing.Annotated`` keeps working."""
        info = catalog.param(
            "typing.Annotated[bool, Field(description='Verbose output.')]"
        )

        assert info["type"] == "bool"
        assert info["description"] == "Verbose output."

    def test_validator_between_type_and_field_is_skipped(self, catalog: Catalog):
        """A bare validator (e.g. JSON_STRING_COERCION) sits between the two."""
        info = catalog.param(
            "Annotated[dict[str, Any] | None, JSON_STRING_COERCION, "
            "Field(description='Extra params.')]"
        )

        assert info["type"] == "dict[str, Any] | None"
        assert info["description"] == "Extra params."

    def test_annotated_nested_in_a_union_rejoins_the_type(self, catalog: Catalog):
        """``Annotated[int, Field(ge=1)] | None`` — the union is the outer node."""
        info = catalog.param("Annotated[int, Field(ge=1)] | None")

        assert info["type"] == "int | None"

    def test_union_keeps_the_description_from_its_annotated_side(
        self, catalog: Catalog
    ):
        info = catalog.param("Annotated[int, Field(description='Page size.')] | None")

        assert info["type"] == "int | None"
        assert info["description"] == "Page size."

    def test_field_default_is_extracted(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str | None, Field(default=None, description='x')]"
        )

        assert info["default"] is None
        assert "constraints" not in info, "a default is not a validation rule"

    def test_plain_annotation_passes_through(self, catalog: Catalog):
        assert catalog.param("str | None") == {"type": "str | None"}

    def test_an_unannotated_parameter_is_required_and_still_listed(
        self, catalog: Catalog
    ):
        """A required name with no property is a schema nothing can render."""
        schema = catalog.schema("p")

        assert schema["properties"]["p"] == {}
        assert schema["required"] == ["p"]


class TestFieldConstraints:
    """``Field`` bounds survive the cleanup of ``type``."""

    def test_numeric_bounds_are_recorded(self, catalog: Catalog):
        info = catalog.param("Annotated[int, Field(ge=1, le=60)]")

        assert info["constraints"] == {"ge": 1, "le": 60}

    def test_exclusive_and_length_bounds(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[list[str], Field(gt=0, lt=5, min_length=1, max_length=3)]"
        )

        assert info["constraints"] == {
            "gt": 0,
            "lt": 5,
            "min_length": 1,
            "max_length": 3,
        }

    def test_length_bounds_are_not_translated_to_json_schema(self, catalog: Catalog):
        """``min_length`` is ``minLength`` on a str but ``minItems`` on a list.

        The annotation is a Python type, so nothing here can tell the two
        apart — translating would mislabel one of them.
        """
        info = catalog.param("Annotated[list[str] | None, Field(min_length=1)]")

        assert set(info["constraints"]) == {"min_length"}

    def test_non_bound_validation_keywords_are_kept_too(self, catalog: Catalog):
        """``strict`` / ``allow_inf_nan`` are validation rules, not decoration.

        They were visible in the annotation source this extractor used to dump
        into ``type``; a whitelist of bounds would drop them silently.
        """
        info = catalog.param(
            "Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]"
        )

        assert info["constraints"] == {
            "ge": 0,
            "strict": True,
            "allow_inf_nan": False,
        }

    def test_negative_bounds_survive(self, catalog: Catalog):
        """``ge=-1`` parses as a unary minus, not a plain literal."""
        info = catalog.param("Annotated[int, Field(ge=-1, le=-0.5)]")

        assert info["constraints"] == {"ge": -1, "le": -0.5}

    def test_bounds_survive_a_union(self, catalog: Catalog):
        info = catalog.param("Annotated[int, Field(ge=1)] | None")

        assert info["type"] == "int | None"
        assert info["constraints"] == {"ge": 1}

    def test_bounds_on_an_optional_branch_inside_annotated_are_read(
        self, catalog: Catalog
    ):
        """Bounds on the list branch, the description on the whole parameter."""
        info = catalog.param(
            "Annotated[Annotated[list[int], Field(min_length=1, max_length=5)]"
            " | None, JSON_STRING_COERCION, Field(description='Edits.')]"
        )

        assert info["type"] == "list[int] | None"
        assert info["constraints"] == {"min_length": 1, "max_length": 5}
        assert info["description"] == "Edits."

    def test_no_constraints_key_without_bounds(self, catalog: Catalog):
        assert "constraints" not in catalog.param(
            "Annotated[str, Field(description='x')]"
        )


class TestUnionMetadata:
    """Metadata belongs to the branch that declared it."""

    def test_metadata_from_the_single_annotated_branch_applies(self, catalog: Catalog):
        info = catalog.param("Annotated[int, Field(ge=1, description='n')] | None")

        assert info["type"] == "int | None"
        assert info["constraints"] == {"ge": 1}
        assert info["description"] == "n"

    def test_metadata_is_not_published_across_a_non_optional_branch(
        self, catalog: Catalog
    ):
        """``ge=1`` says nothing about the ``str`` a caller may pass instead."""
        info = catalog.param("Annotated[int, Field(ge=1)] | str")

        assert info["type"] == "int | str"
        assert "constraints" not in info

    def test_a_three_way_union_publishes_no_metadata(self, catalog: Catalog):
        """``ge`` on the int branch says nothing about the str a caller may pass."""
        info = catalog.param(
            "Annotated[int, Field(ge=1, description='n')] | str | None"
        )

        assert info["type"] == "int | str | None"
        assert "constraints" not in info
        assert "description" not in info

    def test_two_annotated_branches_publish_no_flattened_metadata(
        self, catalog: Catalog
    ):
        """Neither branch's rules govern the whole parameter."""
        info = catalog.param(
            "Annotated[int, Field(ge=1)] | Annotated[str, Field(min_length=1)]"
        )

        assert info["type"] == "int | str"
        assert "constraints" not in info

    def test_same_named_constraints_are_not_silently_overwritten(
        self, catalog: Catalog
    ):
        info = catalog.param(
            "Annotated[int, Field(ge=1)] | Annotated[float, Field(ge=99)]"
        )

        assert "constraints" not in info


class TestZeroArgumentHelpers:
    """Only a helper that can actually be called bare is resolved."""

    def _described_by_docs(self, catalog: Catalog, helper: str) -> dict[str, Any]:
        return catalog.param("Annotated[str, Field(description=docs())]", helper)

    def test_a_truly_zero_argument_helper_resolves(self, catalog: Catalog):
        info = self._described_by_docs(catalog, "def docs():\n    return 'text'\n")

        assert info["description"] == "text"

    def test_a_conditional_return_disqualifies_the_helper(self, catalog: Catalog):
        """Two return paths mean no single value; the last one is not it."""
        helper = "def docs():\n    if enabled:\n        return 'A'\n    return 'B'\n"

        assert "description" not in self._described_by_docs(catalog, helper)

    def test_a_single_conditional_return_disqualifies_the_helper(
        self, catalog: Catalog
    ):
        """Falling past the ``if`` returns None, so the string is not its value."""
        helper = "def docs():\n    if enabled:\n        return 'A'\n"

        assert "description" not in self._described_by_docs(catalog, helper)

    def test_a_keyword_only_parameter_disqualifies_the_helper(self, catalog: Catalog):
        """``docs()`` would raise TypeError, so its return value is not its value."""
        helper = "def docs(*, mode):\n    return 'text'\n"

        assert "description" not in self._described_by_docs(catalog, helper)

    def test_a_positional_only_parameter_disqualifies_the_helper(
        self, catalog: Catalog
    ):
        helper = "def docs(mode, /):\n    return 'text'\n"

        assert "description" not in self._described_by_docs(catalog, helper)

    def test_varargs_disqualify_the_helper(self, catalog: Catalog):
        helper = "def docs(*a, **k):\n    return 'text'\n"

        assert "description" not in self._described_by_docs(catalog, helper)


class TestMetadataSelection:
    """Only ``Field`` keywords describe the parameter."""

    def test_other_metadata_calls_contribute_nothing(self, catalog: Catalog):
        """Their keywords are their own, not pydantic validation rules."""
        info = catalog.param("Annotated[int, OtherMetadata(mode='strict')]")

        assert info == {"type": "int"}

    def test_field_is_still_read_beside_other_metadata(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[int, OtherMetadata(mode='strict'), Field(ge=1)]"
        )

        assert info["constraints"] == {"ge": 1}


class TestRequiredParameters:
    """``required`` must agree with the defaults the schema reports."""

    def test_field_default_alone_makes_a_parameter_optional(self, catalog: Catalog):
        """FastMCP treats ``Field(default=...)`` as the default; so must this."""
        schema = catalog.schema("limit: Annotated[int, Field(default=10)]")

        assert schema["properties"]["limit"]["default"] == 10
        assert "required" not in schema

    def test_a_parameter_with_no_default_at_all_stays_required(self, catalog: Catalog):
        schema = catalog.schema("entity_id: str")

        assert schema["required"] == ["entity_id"]
        assert "default" not in schema["properties"]["entity_id"]

    def test_an_unresolvable_field_default_still_means_optional(self, catalog: Catalog):
        """Requiredness comes from the source, not from what resolved.

        Calling a parameter required because its default could not be read
        states the opposite of the truth to whoever reads the page.
        """
        schema = catalog.schema(
            "limit: Annotated[int, Field(description='n', default=SOME_UNKNOWN)]"
        )

        assert "required" not in schema
        assert "default" not in schema["properties"]["limit"]

    def test_a_signature_default_that_is_a_module_constant_resolves(
        self, catalog: Catalog
    ):
        """The resolver is already in hand; a named default is not a mystery."""
        schema = catalog.schema(
            "width: int = DEFAULT_WIDTH", prelude="DEFAULT_WIDTH = 1280"
        )

        assert schema["properties"]["width"]["default"] == 1280
        assert "required" not in schema

    def test_a_positional_field_default_is_a_default(self, catalog: Catalog):
        """``Field(5)`` and ``Field(default=5)`` mean the same to pydantic."""
        schema = catalog.schema("width: Annotated[int, Field(5, description='W')]")

        assert schema["properties"]["width"]["default"] == 5
        assert "required" not in schema

    def test_field_ellipsis_is_pydantic_s_required_marker_not_a_default(
        self, catalog: Catalog
    ):
        """Both spellings: ``Field(...)`` and ``Field(default=...)``."""
        for annotation in (
            "Annotated[int, Field(..., description='W')]",
            "Annotated[int, Field(default=..., description='W')]",
        ):
            schema = catalog.schema(f"width: {annotation}")

            assert schema["required"] == ["width"], annotation
            assert "default" not in schema["properties"]["width"], annotation

    def test_default_factory_makes_a_parameter_optional(self, catalog: Catalog):
        """pydantic calls the factory when the argument is omitted.

        The callable itself is not published — it is how the value is made,
        not a value, and a function is not JSON.
        """
        schema = catalog.schema(
            "tags: Annotated[list[str], Field(default_factory=list, description='T')]"
        )

        assert "required" not in schema
        assert "default" not in schema["properties"]["tags"]
        assert extract_tools.UNRESOLVED_KEYWORDS == []


class TestUnresolvedValuesAreReported:
    """A value that cannot be read is announced, never quietly dropped."""

    def test_an_unresolvable_constraint_is_recorded(self, catalog: Catalog):
        """The whitelist was removed so nothing goes missing by omission."""
        catalog.param("Annotated[int, Field(ge=CONF_MAX)]")

        assert any(
            "ge=CONF_MAX" in entry for entry in extract_tools.UNRESOLVED_KEYWORDS
        )

    def test_an_unresolvable_positional_default_is_recorded(self, catalog: Catalog):
        catalog.param("Annotated[int, Field(NOT_KNOWN)]")

        assert any("NOT_KNOWN" in entry for entry in extract_tools.UNRESOLVED_KEYWORDS)

    def test_a_deliberately_unresolved_callable_is_not_reported(self, catalog: Catalog):
        """A standing entry every run is what trains a reader to skip the new one."""
        catalog.param("Annotated[int, Field(validation_alias=AliasChoices('a', 'b'))]")
        catalog.param("Annotated[int, Field(default=cast(Any, None))]")

        assert extract_tools.UNRESOLVED_KEYWORDS == []

    def test_a_resolvable_constraint_is_not_recorded(self, catalog: Catalog):
        catalog.param("Annotated[int, Field(ge=1)]")

        assert extract_tools.UNRESOLVED_KEYWORDS == []


class TestMalformedAnnotations:
    """A wrong answer is worse than handing back the source text."""

    def test_a_union_operand_without_a_type_falls_back_to_source(
        self, catalog: Catalog
    ):
        """Joining the rest would name one branch as the whole type."""
        info = catalog.param("Annotated[int] | None")

        assert info["type"] == "Annotated[int] | None"


class TestNonLiteralDescriptions:
    """Descriptions built from constants and helpers still reach the catalog."""

    def test_module_constant_reference(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str, Field(description=_DESC)]",
            prelude="_DESC = 'Initial value.'",
        )

        assert info["description"] == "Initial value."

    def test_fstring_interpolating_constants(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[int, Field(description=f'Capped at {MAX_LIMIT}.')]",
            prelude="MAX_LIMIT = 500",
        )

        assert info["description"] == "Capped at 500."

    def test_fstring_conversions_are_applied(self, catalog: Catalog):
        """``!r`` quotes the value; ignoring it yields a wrong description."""
        info = catalog.param(
            "Annotated[str, Field(description=f'Split on {SEP!r}.')]",
            prelude="SEP = ','",
        )

        assert info["description"] == "Split on ','."

    def test_sliced_and_method_called_constant(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[bool, Field(description=f'{DESC[:1].upper()}{DESC[1:]}.')]",
            prelude="DESC = 'capture the whole page'",
        )

        assert info["description"] == "Capture the whole page."

    def test_concatenation_with_a_zero_arg_helper(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str, Field(description='Transform. '"
            " + get_security_documentation())]",
            prelude="def get_security_documentation():\n    return ' SECURITY: none.'",
        )

        assert info["description"] == "Transform.  SECURITY: none."

    def test_a_constant_that_is_none_is_a_value_not_a_failure(self, catalog: Catalog):
        """``DEFAULT = None`` must reach the schema as a real default."""
        info = catalog.param(
            "Annotated[str | None, Field(default=DEFAULT)]", prelude="DEFAULT = None"
        )

        assert "default" in info
        assert info["default"] is None

    def test_unresolvable_description_is_omitted_not_truncated(self, catalog: Catalog):
        """A partial resolution would silently drop half the documentation."""
        info = catalog.param(
            "Annotated[str, Field(description='Transform. ' + unknown_helper())]"
        )

        assert "description" not in info


class TestForwardReferences:
    """Definition order in the source must not decide what resolves."""

    def test_helper_returning_a_later_constant_resolves(self, catalog: Catalog):
        """A single source-order pass would leave this helper unresolved."""
        info = catalog.param(
            "Annotated[str, Field(description=docs())]",
            prelude="def docs():\n    return TEXT\n\n\nTEXT = 'Shared documentation.'",
        )

        assert info["description"] == "Shared documentation."

    def test_constant_built_from_a_later_constant_resolves(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str, Field(description=HEAD)]",
            prelude="HEAD = LEAD + ' tail.'\n\nLEAD = 'Lead'",
        )

        assert info["description"] == "Lead tail."

    def test_a_genuinely_unresolvable_name_stays_out(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str, Field(description=VALUE)]",
            prelude="VALUE = missing_helper()",
        )

        assert "description" not in info

    def test_a_module_level_assignment_overrides_an_imported_name(
        self, catalog: Catalog
    ):
        """Python's own precedence: the local binding is what the module uses."""
        catalog.module("tools/sibling.py", "NAME = 'imported'\n")
        info = catalog.param(
            "Annotated[str, Field(description=NAME)]",
            prelude="from .sibling import NAME\nNAME = 'local'",
        )

        assert info["description"] == "local"


class TestResolverRejections:
    """The evaluator's boundaries — what it declines to read, and why."""

    def test_a_format_spec_is_not_resolved(self, catalog: Catalog):
        """Formatting is evaluation; the catalog reads, it does not compute."""
        info = catalog.param(
            "Annotated[int, Field(description=f'{MAX:g}')]", prelude="MAX = 500"
        )

        assert "description" not in info

    def test_a_non_slice_subscript_is_not_resolved(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str, Field(description=DESC[0])]", prelude="DESC = 'text'"
        )

        assert "description" not in info

    def test_a_helper_call_with_arguments_is_not_resolved(self, catalog: Catalog):
        info = catalog.param(
            "Annotated[str, Field(description=docs('x'))]",
            prelude="def docs():\n    return 'text'",
        )

        assert "description" not in info

    def test_only_allowlisted_string_methods_run(self, catalog: Catalog):
        """This allowlist is what backs "never executes repository source"."""
        info = catalog.param(
            "Annotated[str, Field(description=DESC.split())]", prelude="DESC = 'a b'"
        )

        assert "description" not in info

    def test_a_field_kwargs_splat_contributes_nothing(self, catalog: Catalog):
        """A splat names no keyword, so there is nothing to publish or report."""
        info = catalog.param("Annotated[int, Field(**COMMON)]")

        assert info == {"type": "int"}
        assert extract_tools.UNRESOLVED_KEYWORDS == []


class TestImportResolution:
    """Every import spelling the package uses, and the containment guard."""

    _DESCRIBED = "Annotated[str, Field(description=NAME)]"

    def test_relative_module_import(self, catalog: Catalog):
        catalog.module("tools/log_common.py", "NAME = 'from a sibling'\n")
        info = catalog.param(self._DESCRIBED, "from .log_common import NAME")

        assert info["description"] == "from a sibling"

    def test_package_initializer_import(self, catalog: Catalog):
        catalog.module("tools/__init__.py", "NAME = 'from the initializer'\n")
        info = catalog.param(self._DESCRIBED, "from . import NAME")

        assert info["description"] == "from the initializer"

    def test_a_sibling_module_import_leaves_the_initializer_unread(
        self, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
    ):
        """``from . import sibling`` binds a module, which holds no name to
        resolve; reading the initializer for it would walk the whole package
        again from every module that imports this way."""
        catalog.module("tools/__init__.py", "NAME = 'from the initializer'\n")
        catalog.module("tools/sibling.py", "NAME = 'from a sibling'\n")
        reads: list[Path] = []
        read_text = Path.read_text

        def counting_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            reads.append(path)
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        catalog.param(
            "Annotated[str, Field(description='text')]", "from . import sibling"
        )

        assert catalog.root / "tools" / "__init__.py" not in reads

    def test_parent_package_import(self, catalog: Catalog):
        catalog.module("utils/python_sandbox.py", "NAME = 'from the parent'\n")
        info = catalog.param(self._DESCRIBED, "from ..utils.python_sandbox import NAME")

        assert info["description"] == "from the parent"

    def test_absolute_intra_package_import(self, catalog: Catalog):
        """The package writes both spellings; both name the same file."""
        catalog.module("utils/python_sandbox.py", "NAME = 'absolute'\n")
        info = catalog.param(
            self._DESCRIBED, "from ha_mcp.utils.python_sandbox import NAME"
        )

        assert info["description"] == "absolute"

    @pytest.mark.parametrize(
        "statement",
        ["from outside import NAME", "from ...outside import NAME"],
        ids=["absolute", "relative"],
    )
    def test_an_import_from_outside_the_package_is_declined(
        self, catalog: Catalog, statement: str
    ):
        """The containment guard is what keeps the extractor out of the world."""
        (catalog.root.parent / "outside.py").write_text("NAME = 'leaked'\n", "utf-8")

        assert "description" not in catalog.param(self._DESCRIBED, statement)

    @pytest.mark.parametrize(
        "statement",
        ["from ha_mcp._vendor.lib import NAME", "from .._vendor.lib import NAME"],
        ids=["absolute", "relative"],
    )
    def test_a_vendored_library_import_is_declined(
        self, catalog: Catalog, statement: str
    ):
        """Vendored libraries live inside the package but are not its tools."""
        catalog.module("_vendor/lib/__init__.py", "NAME = 'vendored'\n")

        assert "description" not in catalog.param(self._DESCRIBED, statement)

    def test_an_aliased_name_is_bound_under_its_local_spelling(self, catalog: Catalog):
        catalog.module("tools/log_common.py", "MAX_LIMIT = 'the cap'\n")
        properties = catalog.schema(
            "capped: Annotated[str, Field(description=CAP)], "
            "original: Annotated[str, Field(description=MAX_LIMIT)]",
            prelude="from .log_common import MAX_LIMIT as CAP",
        )["properties"]

        assert properties["capped"]["description"] == "the cap"
        assert "description" not in properties["original"]


def _described_tool(name: str, statement: str) -> str:
    return (
        f"{statement}\n\n\n@mcp.tool()\n"
        f"async def {name}(p: Annotated[str, Field(description=A_NAME)]) -> None:\n"
        "    pass\n"
    )


class TestImportCycles:
    """A scope built through a cycle is incomplete and must not be cached."""

    def test_no_module_in_a_cycle_is_served_incomplete_later(self, catalog: Catalog):
        """Including the ones that only imported THROUGH the cut.

        a → b → c → a: walked from ``a``, c sees the cycle and lacks
        ``A_NAME``, and b is built from c's partial answer. Caching either
        would serve that incomplete module to every later tool file, so which
        descriptions went missing would depend on file order.
        """
        catalog.module("a.py", "from .b import B_NAME\nA_NAME = 'a'\n")
        catalog.module("b.py", "from .c import C_NAME, A_NAME\nB_NAME = 'b'\n")
        catalog.module("c.py", "from .a import A_NAME\nC_NAME = 'c'\n")

        tools = catalog.tools(
            _described_tool("ha_through_a", "from ..a import A_NAME"),
            _described_tool("ha_through_c", "from ..c import A_NAME"),
            _described_tool("ha_through_b", "from ..b import A_NAME"),
        )

        assert {
            name: tool["inputSchema"]["properties"]["p"].get("description")
            for name, tool in tools.items()
        } == {"ha_through_a": "a", "ha_through_c": "a", "ha_through_b": "a"}

    def test_a_module_is_read_once_per_run(
        self, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
    ):
        """The cache is what keeps the walk from re-expanding per tool file."""
        catalog.module("b.py", "A_NAME = 'b'\n")
        reads: list[Path] = []
        read_text = Path.read_text

        def counting_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            reads.append(path)
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        tools = catalog.tools(
            _described_tool("ha_first", "from ..b import A_NAME"),
            _described_tool("ha_second", "from ..b import A_NAME"),
        )

        assert reads.count(catalog.root / "b.py") == 1
        assert all(
            tool["inputSchema"]["properties"]["p"]["description"] == "b"
            for tool in tools.values()
        )

    def test_an_unreadable_module_degrades_instead_of_crashing(
        self, catalog: Catalog, capsys: pytest.CaptureFixture[str]
    ):
        """One bad file must not abort the catalog — but it must say so."""
        catalog.module("broken.py", "def (\n")

        info = catalog.param(
            "Annotated[str, Field(description=A_NAME)]", "from ..broken import A_NAME"
        )

        assert "description" not in info
        assert "cannot read module scope" in capsys.readouterr().err


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
            "type — the extractor did not recognise the annotation:\n"
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
        statically — across the package's own imports, as the real tool files
        build them — and anything this misses reaches the site as a blank cell.
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
