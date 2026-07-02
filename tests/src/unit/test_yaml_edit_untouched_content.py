"""Regression tests for #1720: ``edit_yaml_config`` must not corrupt
content outside the requested edit.

The handler round-trips the whole target file through ruamel. With the
emitter's default ~80-column width, long lines inside untouched ``>``
folded scalars were re-wrapped on re-serialization; a wrap adjacent to a
more-indented line becomes a literal newline on re-parse, silently
changing quoted Jinja string literals (``strftime('%a %h %d')`` →
``strftime('%a\\n%h %d')``) with no error anywhere.

Covers both layers of the fix:
- the emitter no longer wraps long lines at all (values survive), and
- the handler's round-trip guard refuses to write when re-serialization
  would alter parsed content (simulated by forcing a narrow width).
"""

import asyncio
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import MagicMock as MM

import pytest

# Mock HA imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()

from ruamel.yaml import YAML  # noqa: E402

from custom_components.ha_mcp_tools import (  # noqa: E402
    CALLER_TOKEN_FIELD,
    _build_edit_yaml_config_handler,
)
from custom_components.ha_mcp_tools.const import DOMAIN  # noqa: E402
from custom_components.ha_mcp_tools.yaml_rt import make_yaml  # noqa: E402
from custom_components.ha_mcp_tools.yaml_rt import (  # noqa: E402
    yaml_dumps as real_yaml_dumps,
)

_TEST_CALLER_TOKEN = "test-caller-token-untouched-content"

# Reporter's scenario: a hand-authored template sensor with >-folded Jinja
# blocks whose long, more-indented lines contain quoted strftime literals,
# plus HA tags elsewhere in the file (exercised by the round-trip guard).
ISSUE_1720_CONFIG = """\
default_config:

automation: !include automations.yaml

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

UTILITY_METER_CONTENT = """\
monthly_bill_ac:
  source: sensor.ac_power
  cycle: monthly
daily_bill_ac:
  source: sensor.ac_power
  cycle: daily
"""


@pytest.fixture(autouse=True)
def _stub_config_check(monkeypatch):
    """The post-write config check passes (valid config)."""
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.async_check_ha_config_file",
        AsyncMock(return_value=None),
        raising=False,
    )


@pytest.fixture
def hass(tmp_path):
    """Minimal hass mock that runs executor jobs synchronously."""
    h = MM()
    h.config = MM()
    h.config.config_dir = str(tmp_path)
    h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

    async def _run(fn, *args):
        return fn(*args)

    h.async_add_executor_job = AsyncMock(side_effect=_run)
    h.services = MM()
    h.services.async_call = AsyncMock(return_value=None)
    return h


@pytest.fixture
def call_factory():
    def _make(data):
        call = MM()
        call.data = {**data, CALLER_TOKEN_FIELD: _TEST_CALLER_TOKEN}
        return call

    return _make


def _template_attributes(text: str) -> dict[str, str]:
    data = make_yaml().load(StringIO(text))
    sensor = data["template"][0]["sensor"][0]
    return {k: str(v) for k, v in sensor["attributes"].items()}


def _add_utility_meter_call(call_factory):
    return call_factory(
        {
            "file": "configuration.yaml",
            "action": "add",
            "yaml_path": "utility_meter",
            "content": UTILITY_METER_CONTENT,
        }
    )


class TestUntouchedContentPreserved:
    """add on one key leaves every other parsed value in the file intact."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_unrelated_add_preserves_folded_scalar_values(
        self, tmp_path, hass, call_factory
    ):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)
        before = _template_attributes(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["success"] is True, result
        written = cfg.read_text()
        assert "monthly_bill_ac" in written  # requested edit landed
        assert _template_attributes(written) == before

    def test_long_folded_scalar_line_not_split_on_disk(
        self, tmp_path, hass, call_factory
    ):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["success"] is True, result
        assert (
            ".strftime('%a %h %d') == now().strftime('%a %h %d') %}" in cfg.read_text()
        ), "long folded-scalar line was re-wrapped on disk"


class TestRoundTripGuard:
    """If re-serialization would alter parsed content anywhere in the file,
    the handler must refuse to write rather than corrupt silently."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_write_aborted_when_serialization_would_alter_content(
        self, tmp_path, hass, call_factory, monkeypatch
    ):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        def _narrow_dumps(ry, data):
            # Pre-fix emitter behavior: default ~80-column wrapping.
            narrow = YAML(typ="rt")
            narrow.preserve_quotes = True
            return real_yaml_dumps(narrow, data)

        monkeypatch.setattr("custom_components.ha_mcp_tools.yaml_dumps", _narrow_dumps)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["success"] is False, result
        assert "outside the requested edit" in result["error"]
        # File must be untouched.
        assert cfg.read_text() == ISSUE_1720_CONFIG
