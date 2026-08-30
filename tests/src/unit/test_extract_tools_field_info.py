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
