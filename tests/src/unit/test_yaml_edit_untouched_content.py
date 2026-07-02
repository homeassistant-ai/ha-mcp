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


class TestDiffInResponse:
    """Every write response carries a unified diff of the actual change."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_success_response_includes_diff_and_written(
        self, tmp_path, hass, call_factory
    ):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["success"] is True, result
        assert result["written"] is True
        assert "+utility_meter:" in result["diff"]
        assert "(before)" in result["diff"] and "(after)" in result["diff"]

    def test_pure_add_diff_has_no_removals(self, tmp_path, hass, call_factory):
        """With width + indent-style preservation, adding a new key to an
        HA-docs-style file must not touch any existing line."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        removals = [
            line
            for line in result["diff"].splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        assert removals == [], result["diff"]

    def test_diff_is_truncated_for_huge_changes(self, tmp_path, hass, call_factory):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")
        big = "\n".join(f"meter_{i}:\n  source: sensor.s{i}" for i in range(300))

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "utility_meter",
                        "content": big,
                    }
                )
            )
        )

        assert result["success"] is True, result
        assert "diff truncated" in result["diff"]
        assert len(result["diff"].splitlines()) <= 210


def _confirm_call(call_factory, token=None):
    data = {
        "file": "configuration.yaml",
        "action": "add",
        "yaml_path": "utility_meter",
        "content": UTILITY_METER_CONTENT,
        "require_confirm": True,
    }
    if token is not None:
        data["confirm_token"] = token
    return call_factory(data)


class TestConfirmFlow:
    """require_confirm=True turns the first call into a no-write preview."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_first_call_previews_without_writing(self, tmp_path, hass, call_factory):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_confirm_call(call_factory)))

        assert result["success"] is True, result
        assert result["preview"] is True
        assert result["written"] is False
        assert "+utility_meter:" in result["diff"]
        assert result["confirm_token"]
        assert cfg.read_text() == ISSUE_1720_CONFIG  # nothing written

    def test_confirm_with_token_writes(self, tmp_path, hass, call_factory):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)
        handler = _build_edit_yaml_config_handler(hass)

        preview = self._run(handler(_confirm_call(call_factory)))
        result = self._run(
            handler(_confirm_call(call_factory, token=preview["confirm_token"]))
        )

        assert result["success"] is True, result
        assert result["written"] is True
        assert "monthly_bill_ac" in cfg.read_text()

    def test_stale_token_re_previews(self, tmp_path, hass, call_factory):
        """File changed between preview and confirm → token mismatch, no
        write, fresh token issued."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)
        handler = _build_edit_yaml_config_handler(hass)

        preview = self._run(handler(_confirm_call(call_factory)))
        cfg.write_text(ISSUE_1720_CONFIG + "input_boolean:\n  x: {}\n")
        result = self._run(
            handler(_confirm_call(call_factory, token=preview["confirm_token"]))
        )

        assert result["preview"] is True
        assert result["written"] is False
        assert result["confirm_token_mismatch"] is True
        assert result["confirm_token"] != preview["confirm_token"]
        assert "monthly_bill_ac" not in cfg.read_text()

    def test_absent_flag_keeps_single_call_behavior(self, tmp_path, hass, call_factory):
        """Old servers never send require_confirm — writes stay one-call."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["written"] is True
        assert "monthly_bill_ac" in cfg.read_text()


def _remove_template_call(call_factory, token=None):
    data = {
        "file": "configuration.yaml",
        "action": "remove",
        "yaml_path": "template",
        "require_confirm": True,
    }
    if token is not None:
        data["confirm_token"] = token
    return call_factory(data)


class TestConfirmFlowRemove:
    """remove — the most destructive action — must preview like add/replace."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_remove_previews_without_writing(self, tmp_path, hass, call_factory):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        preview = self._run(handler(_remove_template_call(call_factory)))

        assert preview["preview"] is True, preview
        assert preview["written"] is False
        assert "-template:" in preview["diff"]
        assert cfg.read_text() == ISSUE_1720_CONFIG  # nothing removed

    def test_remove_confirm_applies(self, tmp_path, hass, call_factory):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)
        handler = _build_edit_yaml_config_handler(hass)

        preview = self._run(handler(_remove_template_call(call_factory)))
        result = self._run(
            handler(_remove_template_call(call_factory, token=preview["confirm_token"]))
        )

        assert result["written"] is True, result
        assert "template:" not in cfg.read_text()


# Mixed sequence-indent styles: ruamel supports only one style per dump, so
# the minority style is normalized to the first-detected one — the handler
# must SURFACE that (warning + diff), never let it pass silently (#1720).
MIXED_STYLE_CONFIG = """\
binary_sensor:
  - platform: template
    sensors: {}
sensor:
- platform: rest
- platform: template
"""


class TestMixedStyleReindent:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_mixed_style_add_warns_about_reindent(self, tmp_path, hass, call_factory):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(MIXED_STYLE_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["success"] is True, result
        assert result["written"] is True
        # minority-style block normalized to the dominant (4, 2) style...
        assert "  - platform: rest" in cfg.read_text()
        # ...and the collateral is surfaced, not silent
        assert any("re-indented" in w for w in result.get("warnings", []))

    def test_uniform_style_add_has_no_reindent_warning(
        self, tmp_path, hass, call_factory
    ):
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(handler(_add_utility_meter_call(call_factory)))

        assert result["success"] is True, result
        assert not any("re-indented" in w for w in result.get("warnings", []))

    def test_style_does_not_leak_across_calls(self, tmp_path, hass, call_factory):
        """Two edits on one (thread-cached) instance: the second file keeps
        ITS OWN style, not the first file's."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)  # HA-docs style -> (4, 2)
        pkg = Path(tmp_path) / "packages" / "compact.yaml"
        pkg.parent.mkdir(parents=True)
        pkg.write_text("sensor:\n- platform: rest\n")  # compact -> (2, 0)

        handler = _build_edit_yaml_config_handler(hass)
        first = self._run(handler(_add_utility_meter_call(call_factory)))
        assert first["success"] is True, first
        second = self._run(
            handler(
                call_factory(
                    {
                        "file": "packages/compact.yaml",
                        "action": "add",
                        "yaml_path": "binary_sensor",
                        "content": "- platform: template\n  sensors: {}\n",
                    }
                )
            )
        )

        assert second["success"] is True, second
        assert "- platform: rest" in pkg.read_text().splitlines()  # still col 0


def test_extract_yaml_subtree_ignores_leaked_style():
    """The backup-snapshot subtree extractor must not inherit a sequence
    style a prior edit applied to the thread-cached YAML instance."""
    from custom_components.ha_mcp_tools import _extract_yaml_subtree
    from custom_components.ha_mcp_tools.yaml_rt import apply_seq_indent, make_yaml

    content = "sensor:\n- platform: rest\n- platform: template\n"
    baseline = _extract_yaml_subtree(content, "sensor")
    apply_seq_indent(make_yaml(), (4, 2))  # simulate a prior styled dump
    assert _extract_yaml_subtree(content, "sensor") == baseline
