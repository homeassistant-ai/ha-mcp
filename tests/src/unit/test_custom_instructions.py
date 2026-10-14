"""The owner-set custom_instructions setting in the MCP server instructions."""

from unittest.mock import patch

import pytest

from ha_mcp.config import Settings

from .test_skills_instructions import server  # noqa: F401 - pytest fixture

_TEXT = "Household notes are in /share/llm/home.md; read them before changes."


@pytest.mark.parametrize("value", ["", "   \n  "])
def test_an_unset_setting_adds_nothing_to_the_instructions(server, value):  # noqa: F811
    server.settings.read_only_mode = False
    server.settings.custom_instructions = ""
    with patch.object(server, "_build_skills_instructions", return_value=None):
        baseline = server._build_instructions()
    server.settings.custom_instructions = value
    with patch.object(server, "_build_skills_instructions", return_value=None):
        assert server._build_instructions() == baseline


def test_every_client_receives_the_owners_text(server):  # noqa: F811
    server.settings.read_only_mode = False
    server.settings.custom_instructions = f"  {_TEXT}\n"
    with patch.object(server, "_build_skills_instructions", return_value="SKILLS"):
        instructions = server._build_instructions()
    assert _TEXT in instructions
    assert "## Filing ha-mcp issues" in instructions


def test_the_setting_is_read_from_its_environment_variable(monkeypatch):
    monkeypatch.setenv("HAMCP_CUSTOM_INSTRUCTIONS", _TEXT)
    assert Settings().custom_instructions == _TEXT
