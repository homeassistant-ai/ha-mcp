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
                if arg.annotation is None:
                    continue
                if any(
                    kw.arg == "description"
                    for sub in ast.walk(arg.annotation)
                    if isinstance(sub, ast.Call)
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

    def _scope_for(self, tmp_path, source: str):
        module = tmp_path / "mod.py"
        module.write_text(source, encoding="utf-8")
        scope = extract_tools.ModuleScope({}, {})
        _definition_scan = extract_tools._apply_definitions
        _definition_scan(ast.parse(source), scope)
        return scope

    def test_helper_returning_a_later_constant_resolves(self, tmp_path):
        """A single source-order pass would leave this helper unresolved."""
        scope = self._scope_for(
            tmp_path,
            "def docs():\n    return TEXT\n\n\nTEXT = 'Shared documentation.'\n",
        )

        assert scope.funcs["docs"] == "Shared documentation."

    def test_constant_built_from_a_later_constant_resolves(self, tmp_path):
        scope = self._scope_for(
            tmp_path,
            "HEAD = LEAD + ' tail.'\n\nLEAD = 'Lead'\n",
        )

        assert scope.consts["HEAD"] == "Lead tail."

    def test_a_genuinely_unresolvable_name_stays_out(self, tmp_path):
        scope = self._scope_for(tmp_path, "VALUE = missing_helper()\n")

        assert "VALUE" not in scope.consts


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

        assert "PYTHON TRANSFORM SECURITY" in scope.funcs["get_security_documentation"]


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
            if schema.get("description")
        ]

        assert described, (
            "No extracted parameter carries a description, but tool signatures "
            "use Field(description=...) throughout — extraction is broken."
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
            if (name, param) in extracted
            and not extracted[(name, param)].get("description")
        ]

        assert not undocumented, (
            "Parameters whose signature carries Field(description=...) but whose "
            "text did not resolve:\n"
            + "\n".join(f"  - {entry}" for entry in undocumented)
        )
