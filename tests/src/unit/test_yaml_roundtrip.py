"""Unit tests for yaml_rt round-trip helpers."""

from __future__ import annotations

from io import StringIO

import pytest
from annotatedyaml.loader import FastSafeLoader

from custom_components.ha_mcp_tools.yaml_rt import (
    find_yaml_key_files,
    make_yaml,
    yaml_dumps,
    yaml_jsonify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(text: str):
    """Load YAML text via the round-trip helper and return (ry, data)."""
    ry = make_yaml()
    data = ry.load(StringIO(text))
    return ry, data


# ---------------------------------------------------------------------------
# Comment preservation
# ---------------------------------------------------------------------------


class TestCommentPreservation:
    """Comments (top-level, inline, block) survive a round-trip."""

    YAML_WITH_COMMENTS = """\
# Top-level comment
homeassistant:
  name: Home  # inline comment
  # block comment inside mapping
  unit_system: metric
"""

    def test_top_level_comment_preserved(self):
        ry, data = _load(self.YAML_WITH_COMMENTS)
        out = yaml_dumps(ry, data)
        assert "# Top-level comment" in out

    def test_inline_comment_preserved(self):
        ry, data = _load(self.YAML_WITH_COMMENTS)
        out = yaml_dumps(ry, data)
        assert "# inline comment" in out

    def test_block_comment_preserved(self):
        ry, data = _load(self.YAML_WITH_COMMENTS)
        out = yaml_dumps(ry, data)
        assert "# block comment inside mapping" in out


# ---------------------------------------------------------------------------
# HA custom tags
# ---------------------------------------------------------------------------


class TestSecretTag:
    def test_secret_preserved(self):
        ry, data = _load("api_key: !secret my_api_key\n")
        out = yaml_dumps(ry, data)
        assert "!secret my_api_key" in out

    def test_secret_value(self):
        ry, data = _load("api_key: !secret my_api_key\n")
        assert data["api_key"].tag == "!secret"
        assert data["api_key"].value == "my_api_key"
        assert str(data["api_key"]) == "my_api_key"

    def test_tagged_scalar_equality(self):
        _ry, data = _load("a: !secret key1\nb: !secret key1\nc: !secret key2\n")
        assert data["a"] == data["b"]
        assert data["a"] != data["c"]
        assert data["a"] != "key1"


class TestIncludeTag:
    def test_include_preserved(self):
        ry, data = _load("automations: !include automations.yaml\n")
        out = yaml_dumps(ry, data)
        assert "!include automations.yaml" in out


class TestIncludeDirTags:
    @pytest.mark.parametrize(
        "tag",
        [
            "!include_dir_list",
            "!include_dir_merge_list",
            "!include_dir_named",
            "!include_dir_merge_named",
        ],
    )
    def test_include_dir_tag_preserved(self, tag):
        src = f"items: {tag} ./stuff\n"
        ry, data = _load(src)
        out = yaml_dumps(ry, data)
        assert f"{tag} ./stuff" in out


class TestEnvVarTag:
    def test_env_var_preserved(self):
        ry, data = _load("token: !env_var MY_TOKEN\n")
        out = yaml_dumps(ry, data)
        assert "!env_var MY_TOKEN" in out


class TestCoreTagDiscovery:
    def test_every_tag_core_loads_survives_a_round_trip(self):
        """A tag Core's YAML loader knows is written back as it was read, so an
        edit never drops or resolves one Core would have honoured."""
        tags = [
            tag
            for tag in FastSafeLoader.yaml_constructors
            if isinstance(tag, str) and tag.startswith("!")
        ]
        assert "!input" in tags
        for tag in tags:
            ry, data = _load(f"value: {tag} source\n")
            assert f"{tag} source" in yaml_dumps(ry, data), tag


class TestInputTag:
    """Blueprint ``!input`` markers survive parsing and JSON projection."""

    def test_input_tag_preserved(self):
        ry, data = _load("target: !input target_entity\n")
        assert data["target"].tag == "!input"
        out = yaml_dumps(ry, data)
        assert "!input target_entity" in out
        ry2 = make_yaml()
        data2 = ry2.load(StringIO(out))
        assert data2["target"].tag == "!input"

    def test_unknown_scalar_falls_back_to_str(self):
        class Unknown:
            def __str__(self):
                return "unknown"

        assert yaml_jsonify(Unknown()) == "unknown"

    def test_input_tag_jsonifies_in_source_form(self):
        _ry, data = _load("target: !input target_entity\n")
        assert yaml_jsonify(data) == {"target": "!input target_entity"}


# ---------------------------------------------------------------------------
# Round-trip validity
# ---------------------------------------------------------------------------


class TestRoundTripValidity:
    """Output of a round-trip is itself valid YAML."""

    SAMPLE = """\
# config
homeassistant:
  name: Home  # name
  secrets: !secret db_pass
  includes: !include other.yaml
"""

    def test_output_is_parseable(self):
        ry, data = _load(self.SAMPLE)
        out = yaml_dumps(ry, data)
        # Parse the output again — should not raise
        ry2 = make_yaml()
        data2 = ry2.load(StringIO(out))
        assert "homeassistant" in data2


# ---------------------------------------------------------------------------
# Mutation preserves comments
# ---------------------------------------------------------------------------


class TestMutationPreservesComments:
    SAMPLE = """\
# Main config
homeassistant:
  name: Home  # the name
"""

    def test_adding_key_preserves_comments(self):
        ry, data = _load(self.SAMPLE)
        data["homeassistant"]["new_key"] = "new_value"
        out = yaml_dumps(ry, data)
        assert "# Main config" in out
        assert "# the name" in out
        assert "new_key: new_value" in out


# ---------------------------------------------------------------------------
# Content snippets
# ---------------------------------------------------------------------------


class TestSnippetPreservation:
    """Realistic HA snippet with mixed tags and comments."""

    SNIPPET = """\
# Home Assistant main configuration
homeassistant:
  name: My Home
  latitude: !secret home_lat
  packages: !include_dir_named packages/
  # Enable logging
logger:
  default: warning
"""

    def test_full_snippet_round_trips(self):
        ry, data = _load(self.SNIPPET)
        out = yaml_dumps(ry, data)
        # All comments present
        assert "# Home Assistant main configuration" in out
        assert "# Enable logging" in out
        # Tags present
        assert "!secret home_lat" in out
        assert "!include_dir_named packages/" in out
        # Plain values present
        assert "name: My Home" in out


# ---------------------------------------------------------------------------
# Long-line re-wrapping (#1720)
# ---------------------------------------------------------------------------


class TestNoRewrapOfLongLines:
    """The emitter must never introduce new line breaks on dump (#1720).

    ruamel's default emitter width (~80 columns) re-wraps long lines when
    re-serializing. Inside a ``>`` folded scalar, a new break adjacent to a
    more-indented line becomes a LITERAL newline on re-parse — silently
    corrupting quoted string literals (e.g. ``strftime('%a %h %d')``) in
    blocks the edit never touched.
    """

    # Mirrors the reporter's template sensor: >-folded Jinja blocks whose
    # long, more-indented lines contain quoted strftime format strings.
    ISSUE_1720_YAML = """\
template:
  - sensor:
      - name: reveil boolean
        state: "ok"
        attributes:
          is_alarm_day_boolean: >
            {% set selected = states('sensor.next_alarm_selector') %}
            {% if selected == 'unavailable' %}
              false
            {% else %}
              {% if (selected | as_datetime | as_local).strftime('%a %h %d') == now().strftime('%a %h %d') %}
                    true
              {% else %}
                  false
              {% endif %}
            {% endif %}
          chaudiere_bool: >
            {% set selected = states('sensor.next_alarm_selector') %}
            {% if selected == 'unavailable' %}
              false
            {% else %}
              {% set alarmDate = (now() - timedelta(minutes=20)).strftime('%a %h %d') %}
              {% set nowdate = now().strftime('%a %h %d') %}
              {{ alarmDate == nowdate }}
            {% endif %}
"""

    @staticmethod
    def _attributes(data) -> dict[str, str]:
        sensor = data["template"][0]["sensor"][0]
        return {k: str(v) for k, v in sensor["attributes"].items()}

    def test_unrelated_add_preserves_folded_scalar_values(self):
        """Adding an unrelated top-level key must not change the parsed
        value of any untouched folded-scalar attribute."""
        ry, data = _load(self.ISSUE_1720_YAML)
        before = self._attributes(data)

        data["utility_meter"] = {
            "monthly_bill_ac": {"source": "sensor.ac_power", "cycle": "monthly"}
        }
        out = yaml_dumps(ry, data)

        after = self._attributes(make_yaml().load(StringIO(out)))
        assert after == before

    def test_long_folded_scalar_line_not_split(self):
        """The >80-column strftime comparison line survives the dump on a
        single physical line (no emitter-introduced fold)."""
        ry, data = _load(self.ISSUE_1720_YAML)
        out = yaml_dumps(ry, data)
        assert ".strftime('%a %h %d') == now().strftime('%a %h %d') %}" in out, (
            f"long line was re-wrapped:\n{out}"
        )

    def test_pure_roundtrip_is_semantically_stable(self):
        """Even with no mutation at all, load→dump→load must preserve every
        folded-scalar value."""
        ry, data = _load(self.ISSUE_1720_YAML)
        before = self._attributes(data)
        out = yaml_dumps(ry, data)
        after = self._attributes(make_yaml().load(StringIO(out)))
        assert after == before


# ---------------------------------------------------------------------------
# Sequence-indent style preservation (#1720)
# ---------------------------------------------------------------------------


class TestSeqIndentStylePreservation:
    """Dumps keep the file's own top-level sequence-dash style (#1720
    follow-through: 'the tool changed indentation I did not ask for')."""

    HA_DOCS_STYLE = """\
template:
  - sensor:
      - name: demo
        state: "1"
utility_meter: {}
"""

    COMPACT_STYLE = """\
template:
- sensor:
  - name: demo
    state: "1"
utility_meter: {}
"""

    def test_detects_ha_docs_style(self):
        from custom_components.ha_mcp_tools.yaml_rt import detect_seq_indent

        assert detect_seq_indent(self.HA_DOCS_STYLE) == (4, 2)

    def test_detects_compact_style(self):
        from custom_components.ha_mcp_tools.yaml_rt import detect_seq_indent

        assert detect_seq_indent(self.COMPACT_STYLE) == (2, 0)

    def test_detects_none_without_top_level_sequence(self):
        from custom_components.ha_mcp_tools.yaml_rt import detect_seq_indent

        assert detect_seq_indent("homeassistant:\n  name: Home\n") is None

    def test_ha_docs_style_roundtrips_byte_identical(self):
        from custom_components.ha_mcp_tools.yaml_rt import (
            apply_seq_indent,
            detect_seq_indent,
        )

        ry, data = _load(self.HA_DOCS_STYLE)
        apply_seq_indent(ry, detect_seq_indent(self.HA_DOCS_STYLE))
        assert yaml_dumps(ry, data) == self.HA_DOCS_STYLE

    def test_compact_style_roundtrips_byte_identical(self):
        from custom_components.ha_mcp_tools.yaml_rt import (
            apply_seq_indent,
            detect_seq_indent,
        )

        ry, data = _load(self.COMPACT_STYLE)
        apply_seq_indent(ry, detect_seq_indent(self.COMPACT_STYLE))
        assert yaml_dumps(ry, data) == self.COMPACT_STYLE

    def test_style_reset_between_dumps(self):
        """make_yaml() is thread-cached — applying None must restore the
        compact default rather than leak the previous file's style."""
        from custom_components.ha_mcp_tools.yaml_rt import apply_seq_indent

        ry, data = _load(self.COMPACT_STYLE)
        apply_seq_indent(ry, (4, 2))
        apply_seq_indent(ry, None)
        assert yaml_dumps(ry, data) == self.COMPACT_STYLE


class TestSeqIndentDetectionEdges:
    """detect_seq_indent edge cases: interleaved comments/blanks and
    non-matching (quoted) keys fall through safely."""

    def test_detects_through_comment_and_blank_lines(self):
        from custom_components.ha_mcp_tools.yaml_rt import detect_seq_indent

        text = "template:\n\n  # a comment before the first item\n  - sensor: []\n"
        assert detect_seq_indent(text) == (4, 2)

    def test_quoted_key_falls_back_to_next_key(self):
        from custom_components.ha_mcp_tools.yaml_rt import detect_seq_indent

        text = '"quoted key":\n  - a\nsensor:\n- platform: rest\n'
        assert detect_seq_indent(text) == (2, 0)

    def test_key_with_inline_value_is_skipped(self):
        from custom_components.ha_mcp_tools.yaml_rt import detect_seq_indent

        text = "name: My Home\ntemplate:\n  - sensor: []\n"
        assert detect_seq_indent(text) == (4, 2)


class _AnnotatedDict(dict):
    def __init__(self, path):
        super().__init__()
        self.__config_file__ = path
        self.__line__ = 1

    def __eq__(self, other: object) -> bool:
        return dict.__eq__(self, other)


class TestFindYamlKeyFiles:
    def test_finds_top_level_key_in_config(self):
        root = _AnnotatedDict("/config/configuration.yaml")
        root["template"] = {}

        assert find_yaml_key_files(root, "template") == ["/config/configuration.yaml"]

    def test_finds_nested_package_mapping(self):
        root = _AnnotatedDict("/config/configuration.yaml")
        packages = _AnnotatedDict("/config/configuration.yaml")
        nested = _AnnotatedDict("/config/custom_packages/nested/mcp.yaml")
        nested["template"] = {}
        packages["nested"] = nested
        root["custom_packages"] = packages

        assert find_yaml_key_files(root, "template") == [
            "/config/custom_packages/nested/mcp.yaml"
        ]

    def test_dotted_path_uses_the_containing_mapping(self):
        root = _AnnotatedDict("/config/configuration.yaml")
        home = _AnnotatedDict("/config/configuration.yaml")
        home["name"] = "Home"
        root["homeassistant"] = home

        assert find_yaml_key_files(root, "homeassistant.name") == [
            "/config/configuration.yaml"
        ]


class _TaggedValue:
    """A resolved value annotated with the file that supplied it."""

    def __init__(self, path):
        self.__config_file__ = path
        self.__line__ = 1


class TestFindYamlKeyEdges:
    def test_empty_path_returns_nothing(self):
        assert find_yaml_key_files({"template": {}}, "") == []

    def test_scalar_root_returns_nothing(self):
        assert find_yaml_key_files("not a mapping", "template") == []

    def test_list_root_walks_mappings(self):
        node = _AnnotatedDict("/config/configuration.yaml")
        node["template"] = {}
        assert find_yaml_key_files([node], "template") == ["/config/configuration.yaml"]

    def test_empty_path_segment_is_not_collapsed(self):
        """``sensor..name`` must not be read as ``sensor.name``."""
        root = _AnnotatedDict("/config/configuration.yaml")
        root["sensor"] = {"name": "x"}

        assert find_yaml_key_files(root, "sensor..name") == []

    def test_keys_nested_in_a_secret_mapping_are_not_discovered(self):
        """A mapping-valued !secret is not a config file root."""
        root = _AnnotatedDict("/config/configuration.yaml")
        secret = _AnnotatedDict("/config/secrets.yaml")
        secret["template"] = {}
        root["shell_command"] = secret

        assert find_yaml_key_files(root, "template") == []

    def test_key_nested_below_a_top_level_mapping_is_not_a_root(self):
        """A nested ``sensor`` under ``template`` is not a top-level sensor."""
        root = _AnnotatedDict("/config/configuration.yaml")
        template = _AnnotatedDict("/config/configuration.yaml")
        template["sensor"] = []
        root["template"] = template

        assert find_yaml_key_files(root, "sensor") == []

    def test_mapping_without_source_annotation_is_ignored(self):
        assert find_yaml_key_files({"template": {}}, "template") == []

    def test_path_across_an_include_is_not_attributed_to_the_parent(self):
        """The parent file cannot supply a path that continues in an include."""
        root = _AnnotatedDict("/config/configuration.yaml")
        included = _AnnotatedDict("/config/commands.yaml")
        included["mcp_secret"] = "value"
        root["shell_command"] = included

        assert find_yaml_key_files(root, "shell_command.mcp_secret") == []

    def test_included_mapping_is_found_at_its_own_path(self):
        """The included file is still discoverable by its own top-level path."""
        root = _AnnotatedDict("/config/configuration.yaml")
        included = _AnnotatedDict("/config/commands.yaml")
        included["shell_command"] = {"mcp_secret": "value"}
        root["included"] = included

        assert find_yaml_key_files(root, "shell_command.mcp_secret") == [
            "/config/commands.yaml"
        ]

    def test_key_uses_the_mapping_not_its_secret_value(self):
        root = _AnnotatedDict("/config/configuration.yaml")
        package = _AnnotatedDict("/config/custom_packages/nested/mcp.yaml")
        package["shell_command"] = {"mcp_secret": _TaggedValue("/config/secrets.yaml")}
        root["custom_packages"] = {"nested": package}

        assert find_yaml_key_files(root, "shell_command.mcp_secret") == [
            "/config/custom_packages/nested/mcp.yaml"
        ]

    def test_cycles_are_walked_once(self):
        node = _AnnotatedDict("/config/configuration.yaml")
        node["self"] = node
        node["template"] = {}
        assert find_yaml_key_files(node, "template") == ["/config/configuration.yaml"]
