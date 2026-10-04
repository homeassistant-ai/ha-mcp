"""The ``*~`` path segment: match the keys of an object argument.

A scene's ``config.entities`` names the entities it touches by key
(``{"lock.front_door": "unlocked"}``), so a value-only path cannot express
"gate scenes that include a lock". ``*~`` fans out over a dict's keys the way
``*`` fans out over its values.
"""

import pytest

from ha_mcp.policy.evaluator import (
    MISSING,
    Verdict,
    evaluate,
    iter_path_values,
    normalize_stringified_containers,
)
from ha_mcp.policy.model import Policy, Predicate, Rule

SENSITIVE = r"^(lock|siren|alarm_control_panel)\."

SCENE_KEYS = Rule(
    tool_name="ha_config_set_scene",
    when=[Predicate(path="args.config.entities.*~", op="regex", value=SENSITIVE)],
)


def _scene(entities: object) -> dict:
    return {"scene_id": "movie", "config": {"name": "Movie", "entities": entities}}


class TestIterPathKeys:
    def test_yields_dict_keys(self):
        args = _scene({"light.living": "on", "lock.front": "unlocked"})
        assert list(iter_path_values(args, "args.config.entities.*~")) == [
            "light.living",
            "lock.front",
        ]

    def test_values_wildcard_does_not_see_keys(self):
        args = _scene({"lock.front": "unlocked"})
        assert list(iter_path_values(args, "args.config.entities.*")) == ["unlocked"]

    @pytest.mark.parametrize("node", [["lock.front"], "lock.front", 3, None, {}])
    def test_list_scalar_or_empty_yields_nothing(self, node):
        assert list(iter_path_values({"x": node}, "args.x.*~")) == []

    @pytest.mark.parametrize("node", [["lock.front"], "lock.front", {}])
    def test_report_missing_flags_a_node_without_keys(self, node):
        assert list(
            iter_path_values({"x": node}, "args.x.*~", report_missing=True)
        ) == [MISSING]

    def test_top_level_keys_are_argument_names(self):
        assert sorted(iter_path_values({"a": 1, "b": 2}, "args.*~")) == ["a", "b"]


class TestRequireApprovalListOnSceneKeys:
    policy = Policy(rules=[SCENE_KEYS])

    @pytest.mark.parametrize(
        "entities",
        [
            {"lock.front": "unlocked"},
            {"light.living": "on", "siren.patio": "off"},
            {"alarm_control_panel.home": {"state": "disarmed"}},
            {"Lock.Front": "unlocked"},
        ],
    )
    def test_scene_with_a_sensitive_entity_needs_approval(self, entities):
        verdict = evaluate("ha_config_set_scene", _scene(entities), self.policy)
        assert verdict == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "args",
        [
            _scene({"light.living": "off", "cover.blind": "open"}),
            _scene({"switch.lock_screen": "on", "sensor.siren_battery": "90"}),
            _scene({"light.living": "lock.front"}),
            {"scene_id": "movie", "config": {"name": "No entities"}},
            {"scene_id": "movie"},
        ],
    )
    def test_other_scenes_run(self, args):
        assert evaluate("ha_config_set_scene", args, self.policy) == Verdict.ALLOW

    def test_stringified_config_is_inspected(self):
        args = normalize_stringified_containers(
            {"scene_id": "movie", "config": '{"entities": {"lock.front": "unlocked"}}'}
        )
        verdict = evaluate("ha_config_set_scene", args, self.policy)
        assert verdict == Verdict.REQUIRE_APPROVAL


class TestAllowListOnSceneKeys:
    policy = Policy(
        rule_effect="allow",
        rules=[
            Rule(
                tool_name="ha_config_set_scene",
                when=[
                    Predicate(
                        path="args.config.entities.*~", op="regex", value=r"^light\."
                    )
                ],
            )
        ],
    )

    def test_scene_of_only_listed_entities_runs(self):
        args = _scene({"light.living": "on", "light.hall": "off"})
        assert evaluate("ha_config_set_scene", args, self.policy) == Verdict.ALLOW

    @pytest.mark.parametrize(
        "entities",
        [
            {"light.living": "on", "lock.front": "unlocked"},
            {},
            [{"entity_id": "light.living"}],
            {" light.living": "on"},
        ],
    )
    def test_anything_else_needs_approval(self, entities):
        verdict = evaluate("ha_config_set_scene", _scene(entities), self.policy)
        assert verdict == Verdict.REQUIRE_APPROVAL
