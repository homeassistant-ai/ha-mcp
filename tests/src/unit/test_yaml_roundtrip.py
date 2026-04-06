"""Unit tests for yaml_rt round-trip helpers."""

from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import MagicMock

import pytest

# Mock Home Assistant imports so the package __init__ can be loaded.
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()

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
    @pytest.mark.parametrize("tag", [
        "!include_dir_list",
        "!include_dir_merge_list",
        "!include_dir_named",
    ])
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
