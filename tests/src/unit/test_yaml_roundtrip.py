"""Unit tests for yaml_rt round-trip helpers."""

from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import MagicMock

import pytest

# Mock Home Assistant imports so the package __init__ can be loaded.
sys.modules["voluptuous"] = MagicMock()
homeassistant = MagicMock()
sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.components"] = homeassistant.components
sys.modules["homeassistant.config"] = homeassistant.config
sys.modules["homeassistant.config_entries"] = homeassistant.config_entries
sys.modules["homeassistant.core"] = homeassistant.core
sys.modules["homeassistant.helpers"] = homeassistant.helpers
sys.modules["homeassistant.helpers.config_validation"] = (
    homeassistant.helpers.config_validation
)
# The package __init__ also imports these; without them this file only
# collects cleanly when an earlier test file has already mocked them.
sys.modules["homeassistant.components.persistent_notification"] = (
    homeassistant.components.persistent_notification
)
sys.modules["homeassistant.helpers.storage"] = homeassistant.helpers.storage
sys.modules["homeassistant.loader"] = homeassistant.loader

from custom_components.ha_mcp_tools.yaml_rt import (  # noqa: E402
    _TaggedScalar,
    make_yaml,
    yaml_dumps,
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
        assert isinstance(data["api_key"], _TaggedScalar)
        assert data["api_key"].tag == "!secret"
        assert data["api_key"].value == "my_api_key"

    def test_tagged_scalar_str(self):
        ts = _TaggedScalar("!secret", "my_api_key")
        assert str(ts) == "my_api_key"

    def test_tagged_scalar_equality(self):
        a = _TaggedScalar("!secret", "key1")
        b = _TaggedScalar("!secret", "key1")
        c = _TaggedScalar("!secret", "key2")
        assert a == b
        assert a != c
        assert a != "key1"


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
