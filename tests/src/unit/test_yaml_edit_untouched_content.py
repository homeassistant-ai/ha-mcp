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

The service runs on a real ``hass`` whose config directory is the test's
``tmp_path``.
"""

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from ruamel.yaml import YAML

from custom_components.ha_mcp_tools import (
    CALLER_TOKEN_FIELD,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_READ_FILE,
)
from custom_components.ha_mcp_tools.const import DOMAIN
from custom_components.ha_mcp_tools.yaml_rt import make_yaml
from custom_components.ha_mcp_tools.yaml_rt import (
    yaml_dumps as real_yaml_dumps,
)

from ._edit_yaml import EditYaml, make_edit_yaml

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


@pytest.fixture
async def edit_yaml(hass: HomeAssistant) -> EditYaml:
    """Call ``ha_mcp_tools.edit_yaml_config`` the way ha-mcp does."""
    return await make_edit_yaml(hass)


def _template_attributes(text: str) -> dict[str, str]:
    data = make_yaml().load(StringIO(text))
    sensor = data["template"][0]["sensor"][0]
    return {k: str(v) for k, v in sensor["attributes"].items()}


ADD_UTILITY_METER = {
    "file": "configuration.yaml",
    "action": "add",
    "yaml_path": "utility_meter",
    "content": UTILITY_METER_CONTENT,
}


class TestUntouchedContentPreserved:
    """add on one key leaves every other parsed value in the file intact."""

    async def test_unrelated_add_preserves_folded_scalar_values(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)
        before = _template_attributes(ISSUE_1720_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["success"] is True, result
        written = cfg.read_text()
        assert "monthly_bill_ac" in written  # requested edit landed
        assert _template_attributes(written) == before

    async def test_long_folded_scalar_line_not_split_on_disk(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["success"] is True, result
        assert (
            ".strftime('%a %h %d') == now().strftime('%a %h %d') %}" in cfg.read_text()
        ), "long folded-scalar line was re-wrapped on disk"


class TestRoundTripGuard:
    """If re-serialization would alter parsed content anywhere in the file,
    the handler must refuse to write rather than corrupt silently."""

    async def test_write_aborted_when_serialization_would_alter_content(
        self, tmp_path: Path, edit_yaml: EditYaml, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        def _narrow_dumps(ry: YAML, data: Any) -> str:
            # Pre-fix emitter behavior: default ~80-column wrapping.
            narrow = YAML(typ="rt")
            narrow.preserve_quotes = True
            return real_yaml_dumps(narrow, data)

        monkeypatch.setattr("custom_components.ha_mcp_tools.yaml_dumps", _narrow_dumps)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["success"] is False, result
        assert "outside the requested edit" in result["error"]
        # File must be untouched.
        assert cfg.read_text() == ISSUE_1720_CONFIG


class TestDiffInResponse:
    """Every write response carries a unified diff of the actual change."""

    async def test_success_response_includes_diff_and_written(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["success"] is True, result
        assert result["written"] is True
        assert "+utility_meter:" in result["diff"]
        assert "(before)" in result["diff"] and "(after)" in result["diff"]

    async def test_pure_add_diff_has_no_removals(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """With width + indent-style preservation, adding a new key to an
        HA-docs-style file must not touch any existing line."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        removals = [
            line
            for line in result["diff"].splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        assert removals == [], result["diff"]

    async def test_diff_is_truncated_for_huge_changes(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text("default_config:\n")
        big = "\n".join(f"meter_{i}:\n  source: sensor.s{i}" for i in range(300))

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "utility_meter",
                "content": big,
            }
        )

        assert result["success"] is True, result
        assert "diff truncated" in result["diff"]
        assert len(result["diff"].splitlines()) <= 210


def _confirm_call(token: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {**ADD_UTILITY_METER, "require_confirm": True}
    if token is not None:
        data["confirm_token"] = token
    return data


class TestConfirmFlow:
    """require_confirm=True turns the first call into a no-write preview."""

    async def test_first_call_previews_without_writing(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        result = await edit_yaml(_confirm_call())

        assert result["success"] is True, result
        assert result["preview"] is True
        assert result["written"] is False
        assert "+utility_meter:" in result["diff"]
        assert result["confirm_token"]
        assert cfg.read_text() == ISSUE_1720_CONFIG  # nothing written

    async def test_confirm_with_token_writes(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        preview = await edit_yaml(_confirm_call())
        result = await edit_yaml(_confirm_call(preview["confirm_token"]))

        assert result["success"] is True, result
        assert result["written"] is True
        assert "monthly_bill_ac" in cfg.read_text()

    async def test_stale_token_re_previews(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """File changed between preview and confirm → token mismatch, no
        write, fresh token issued."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        preview = await edit_yaml(_confirm_call())
        cfg.write_text(ISSUE_1720_CONFIG + "input_boolean:\n  x: {}\n")
        result = await edit_yaml(_confirm_call(preview["confirm_token"]))

        assert result["preview"] is True
        assert result["written"] is False
        assert result["confirm_token_mismatch"] is True
        assert result["confirm_token"] != preview["confirm_token"]
        assert "monthly_bill_ac" not in cfg.read_text()

    async def test_absent_flag_keeps_single_call_behavior(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        """Old servers never send require_confirm — writes stay one-call."""
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["written"] is True
        assert "monthly_bill_ac" in cfg.read_text()


def _remove_template_call(token: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "file": "configuration.yaml",
        "action": "remove",
        "yaml_path": "template",
        "require_confirm": True,
    }
    if token is not None:
        data["confirm_token"] = token
    return data


class TestConfirmFlowRemove:
    """remove — the most destructive action — must preview like add/replace."""

    async def test_remove_previews_without_writing(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        preview = await edit_yaml(_remove_template_call())

        assert preview["preview"] is True, preview
        assert preview["written"] is False
        assert "-template:" in preview["diff"]
        assert cfg.read_text() == ISSUE_1720_CONFIG  # nothing removed

    async def test_remove_confirm_applies(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        preview = await edit_yaml(_remove_template_call())
        result = await edit_yaml(_remove_template_call(preview["confirm_token"]))

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
    async def test_mixed_style_add_warns_about_reindent(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(MIXED_STYLE_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["success"] is True, result
        assert result["written"] is True
        # minority-style block normalized to the dominant (4, 2) style...
        assert "  - platform: rest" in cfg.read_text()
        # ...and the collateral is surfaced, not silent
        assert any("re-indented" in w for w in result.get("warnings", []))

    async def test_uniform_style_add_has_no_reindent_warning(
        self, tmp_path: Path, edit_yaml: EditYaml
    ) -> None:
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)

        result = await edit_yaml(ADD_UTILITY_METER)

        assert result["success"] is True, result
        assert not any("re-indented" in w for w in result.get("warnings", []))

    async def test_style_does_not_leak_across_calls(
        self, tmp_path: Path, edit_yaml: EditYaml, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two edits on one (thread-cached) instance: the second file keeps
        ITS OWN style, not the first file's."""
        # Core's executor may run the two edits on different threads, each
        # with its own cached instance; one instance makes them share it.
        shared = make_yaml()
        monkeypatch.setattr("custom_components.ha_mcp_tools.make_yaml", lambda: shared)
        cfg = tmp_path / "configuration.yaml"
        cfg.write_text(ISSUE_1720_CONFIG)  # HA-docs style -> (4, 2)
        pkg = tmp_path / "packages" / "compact.yaml"
        pkg.parent.mkdir(parents=True)
        pkg.write_text("sensor:\n- platform: rest\n")  # compact -> (2, 0)

        first = await edit_yaml(ADD_UTILITY_METER)
        assert first["success"] is True, first
        second = await edit_yaml(
            {
                "file": "packages/compact.yaml",
                "action": "add",
                "yaml_path": "binary_sensor",
                "content": "- platform: template\n  sensors: {}\n",
            }
        )

        assert second["success"] is True, second
        assert "- platform: rest" in pkg.read_text().splitlines()  # still col 0


async def test_read_subtree_ignores_leaked_style(
    hass: HomeAssistant,
    edit_yaml: EditYaml,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backup snapshot ``read_file`` takes of a key must not inherit a
    sequence style a prior edit applied to the thread-cached YAML instance."""
    from custom_components.ha_mcp_tools.yaml_rt import apply_seq_indent

    # Core's executor may serve the two reads on different threads, each with
    # its own cached instance; one instance makes them share it.
    shared = make_yaml()
    monkeypatch.setattr("custom_components.ha_mcp_tools.make_yaml", lambda: shared)
    (tmp_path / "configuration.yaml").write_text(
        "sensor:\n- platform: rest\n- platform: template\n"
    )
    bootstrap = await hass.services.async_call(
        DOMAIN, SERVICE_GET_CALLER_TOKEN, {}, blocking=True, return_response=True
    )
    assert bootstrap is not None

    async def subtree() -> Any:
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_READ_FILE,
            {
                "path": "configuration.yaml",
                "yaml_path": "sensor",
                CALLER_TOKEN_FIELD: bootstrap["token"],
            },
            blocking=True,
            return_response=True,
        )
        assert response is not None and response["success"] is True, response
        return response["subtree"]

    baseline = await subtree()
    apply_seq_indent(shared, (4, 2))  # simulate a prior styled dump
    assert await subtree() == baseline
