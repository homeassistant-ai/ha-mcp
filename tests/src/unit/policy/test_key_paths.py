"""The ``*~`` path segment: match the keys of an object argument.

A scene's ``config.entities`` names the entities it touches by key
(``{"lock.front_door": "unlocked"}``), so a value-only path cannot express
"gate scenes that include a lock". ``*~`` fans out over a dict's keys the way
``*`` fans out over its values.
"""

import pytest
from pydantic import ValidationError

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


BLOCK_LIST = Policy(rules=[SCENE_KEYS])


def _allow_list(regex: str) -> Policy:
    return Policy(
        rule_effect="allow",
        rules=[
            Rule(
                tool_name="ha_config_set_scene",
                when=[
                    Predicate(path="args.config.entities.*~", op="regex", value=regex)
                ],
            )
        ],
    )


LIGHTS_ONLY = _allow_list(r"^light\.")


@pytest.mark.parametrize(
    ("policy", "entities"),
    [
        (BLOCK_LIST, {"lock.front": "unlocked"}),
        (BLOCK_LIST, {"light.living": "on", "siren.patio": "off"}),
        (BLOCK_LIST, {"alarm_control_panel.home": {"state": "disarmed"}}),
        (BLOCK_LIST, {"Lock.Front": "unlocked"}),
        (LIGHTS_ONLY, {"light.living": "on", "lock.front": "unlocked"}),
        (LIGHTS_ONLY, {}),
        (LIGHTS_ONLY, [{"entity_id": "light.living"}]),
        # Unanchored, so only the whitespace check can refuse the padded key.
        (_allow_list(r"light\."), {" light.living": "on"}),
    ],
)
def test_scene_needs_approval(policy, entities):
    verdict = evaluate("ha_config_set_scene", _scene(entities), policy)
    assert verdict == Verdict.REQUIRE_APPROVAL


class TestRequireApprovalListOnSceneKeys:
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
        assert evaluate("ha_config_set_scene", args, BLOCK_LIST) == Verdict.ALLOW

    def test_stringified_config_is_inspected(self):
        args = normalize_stringified_containers(
            {"scene_id": "movie", "config": '{"entities": {"lock.front": "unlocked"}}'}
        )
        verdict = evaluate("ha_config_set_scene", args, BLOCK_LIST)
        assert verdict == Verdict.REQUIRE_APPROVAL


class TestAllowListOnSceneKeys:
    def test_scene_of_only_listed_entities_runs(self):
        args = _scene({"light.living": "on", "light.hall": "off"})
        assert evaluate("ha_config_set_scene", args, LIGHTS_ONLY) == Verdict.ALLOW

    def test_padded_key_matches_an_unanchored_regex(self):
        # Guards the case above: without the whitespace check it would run.
        assert list(
            iter_path_values(_scene({" light.living": "on"}), "args.config.entities.*~")
        ) == [" light.living"]


class TestKeysSegmentMustBeLast:
    @pytest.mark.parametrize(
        "path", ["args.config.entities.*~.state", "args.*~.entity_id", "*~.*"]
    )
    def test_segment_after_keys_is_rejected(self, path):
        with pytest.raises(ValidationError, match="last segment"):
            Predicate(path=path, op="exists")

    @pytest.mark.parametrize("path", ["args.config.entities.*~", "args.*~", "*~"])
    def test_keys_as_last_segment_is_accepted(self, path):
        assert Predicate(path=path, op="exists").path == path
