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


def _capture(policy: Policy, entities: object = None, **capture: object) -> Verdict:
    config: dict[str, object] = {"name": "Movie", "entities": {"light.living": "on"}}
    if entities is not None:
        config["entities"] = entities
    args = {"scene_id": "movie", "config": config, **capture}
    return evaluate("ha_config_set_scene", args, policy)


class TestCapturedEntities:
    """``capture_entities`` adds its ids to ``config.entities`` inside the tool,
    so a rule on those keys reads them as keys too, named as Core stores them."""

    @pytest.mark.parametrize("captured", ["lock.front", "LOCK.FRONT"])
    def test_a_captured_lock_needs_approval(self, captured):
        assert (
            _capture(BLOCK_LIST, capture_entities=[captured])
            == Verdict.REQUIRE_APPROVAL
        )

    @pytest.mark.parametrize("captured", ["lock.front", "LOCK.FRONT"])
    def test_a_captured_lock_rides_no_lights_only_approval(self, captured):
        assert (
            _capture(LIGHTS_ONLY, capture_entities=[captured])
            == Verdict.REQUIRE_APPROVAL
        )

    @pytest.mark.parametrize("captured", ["light.hall", "LIGHT.HALL"])
    def test_a_captured_light_runs_under_both_lists(self, captured):
        assert _capture(BLOCK_LIST, capture_entities=[captured]) == Verdict.ALLOW
        assert _capture(LIGHTS_ONLY, capture_entities=[captured]) == Verdict.ALLOW

    def test_a_wildcard_for_config_on_the_entity_keys_is_judged_as_usual(self):
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[Predicate(path="args.*.entities.*~", op="regex", value=SENSITIVE)],
        )
        policy = Policy(rules=[rule])
        assert _capture(policy, capture_entities=["light.hall"]) == Verdict.ALLOW
        assert (
            _capture(policy, capture_entities=["lock.front"])
            == Verdict.REQUIRE_APPROVAL
        )

    @pytest.mark.parametrize(
        ("entities", "capture"),
        [
            (None, {"capture_entities": {"lock.front": {}}}),
            (None, {"capture_entities": [1, None]}),
            ([{"entity_id": "light.living"}], {"capture_entities": ["lock.front"]}),
        ],
    )
    def test_a_malformed_capture_is_left_to_the_tool(self, entities, capture):
        assert _capture(BLOCK_LIST, entities, **capture) == Verdict.ALLOW

    @pytest.mark.parametrize(
        ("path", "effect", "expected"),
        [
            ("args", "require_approval", Verdict.REQUIRE_APPROVAL),
            ("args.config", "require_approval", Verdict.REQUIRE_APPROVAL),
            ("args.config.entities", "require_approval", Verdict.REQUIRE_APPROVAL),
            ("args", "allow", Verdict.ALLOW),
            ("args.config", "allow", Verdict.REQUIRE_APPROVAL),
            ("args.config.entities", "allow", Verdict.REQUIRE_APPROVAL),
        ],
    )
    def test_a_rule_on_a_whole_object_holds_the_capture(self, path, effect, expected):
        """A require-approval rule naming the call as written still gates it.
        Under an allow list only the whole argument object, which lists
        ``capture_entities`` itself, approves the capture; a rule on ``config``
        or ``config.entities`` reaches what the capture writes and never does."""
        entities = {"light.living": "on"}
        config = {"name": "Movie", "entities": entities}
        args = {
            "scene_id": "movie",
            "config": config,
            "capture_entities": ["lock.front"],
        }
        exact = {"args": args, "args.config": config}.get(path, entities)
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[Predicate(path=path, op="eq", value=exact)],
        )
        verdict = evaluate(
            "ha_config_set_scene", args, Policy(rules=[rule], rule_effect=effect)
        )
        assert verdict == expected

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("args.config.entities.*.state", "unlocked"),
            ("args.*.entities.*", "unlocked"),
            ("args.config.metadata.*~", "lock.front"),
            ("args.config.*~", "metadata"),
        ],
    )
    def test_a_rule_on_what_the_capture_writes_gates_it(self, path, value):
        """The captured state, the ``entity_only`` mark and the ``metadata`` key
        holding it are written inside the tool, after the gate, so a rule that
        reads them gates the call."""
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[Predicate(path=path, op="eq", value=value)],
        )
        verdict = _capture(Policy(rules=[rule]), capture_entities=["lock.front"])
        assert verdict == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize("path", ["args.config.entities.*", "args.config.metadata"])
    def test_an_allow_list_rule_on_what_the_capture_writes_does_not_approve_it(
        self, path
    ):
        """``{}`` stands for neither the live state the tool stores nor the
        metadata it adds for the lock."""
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[Predicate(path=path, op="eq", value={})],
        )
        args = {
            "scene_id": "movie",
            "config": {"name": "Movie", "entities": {}, "metadata": {}},
            "capture_entities": ["lock.front"],
        }
        policy = Policy(rules=[rule], rule_effect="allow")
        assert evaluate("ha_config_set_scene", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_an_allow_list_rule_on_the_config_keys_does_not_approve_it(self):
        """The tool adds ``metadata`` to ``config`` after the gate."""
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[
                Predicate(path="args.config.*~", op="in", value=["name", "entities"])
            ],
        )
        policy = Policy(rules=[rule], rule_effect="allow")
        assert (
            _capture(policy, capture_entities=["lock.front"])
            == Verdict.REQUIRE_APPROVAL
        )

    def test_a_key_condition_does_not_exempt_a_value_condition_beside_it(self):
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[
                Predicate(path="args.config.entities.*~", op="regex", value="^lock\\."),
                Predicate(path="args.config.entities.*.state", op="eq", value="on"),
            ],
        )
        verdict = _capture(Policy(rules=[rule]), {}, capture_entities=["lock.front"])
        assert verdict == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "when",
        [
            SCENE_KEYS.when,
            [Predicate(path="args.config.entities.*", op="eq", value="on")],
        ],
    )
    def test_another_tools_capture_argument_is_not_read_as_scene_entities(self, when):
        rule = Rule(tool_name="*", when=when)
        args = {"config": {"entities": {}}, "capture_entities": ["lock.front"]}
        verdict = evaluate("ha_config_set_script", args, Policy(rules=[rule]))
        assert verdict == Verdict.ALLOW

    def test_a_capture_without_a_config_object_is_left_to_the_tool(self):
        args = {"scene_id": "movie", "config": None, "capture_entities": ["light.x"]}
        assert evaluate("ha_config_set_scene", args, BLOCK_LIST) == Verdict.ALLOW


class TestCapturedDevices:
    """A device's entities are read inside the tool, after the gate, so a rule
    that inspects ``config.entities`` or ``config.metadata`` cannot see them: it
    gates the call under a require-approval list and cannot approve it under an
    allow list."""

    @pytest.mark.parametrize(
        ("tool", "path"),
        [
            ("ha_config_set_scene", "args.config.entities.*~"),
            ("ha_config_set_scene", "args.*.entities.*~"),
            ("ha_config_set_scene", "config.*"),
            ("ha_config_set_scene", "args.config.metadata.*~"),
            ("*", "args.config.entities.*~"),
        ],
    )
    def test_a_rule_on_scene_entities_gates_a_device_capture(self, tool, path):
        rule = Rule(
            tool_name=tool, when=[Predicate(path=path, op="regex", value=SENSITIVE)]
        )
        policy = Policy(rules=[rule])
        assert _capture(policy, capture_devices=["d1"]) == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        ("effect", "approved_scene"),
        [("allow", "movie"), ("require_approval", "other")],
    )
    def test_a_rule_on_the_whole_argument_object_is_judged_as_written(
        self, effect, approved_scene
    ):
        args = {
            "scene_id": "movie",
            "config": {"name": "Movie", "entities": {}},
            "capture_devices": ["d1"],
        }
        approved = {**args, "scene_id": approved_scene}
        rule = Rule(
            tool_name="ha_config_set_scene",
            when=[Predicate(path="args", op="in", value=[approved])],
        )
        policy = Policy(rules=[rule], rule_effect=effect)
        assert evaluate("ha_config_set_scene", args, policy) == Verdict.ALLOW

    def test_a_lights_only_approval_does_not_cover_a_device_capture(self):
        assert _capture(LIGHTS_ONLY, capture_devices=["d1"]) == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "rule",
        [
            Rule(
                tool_name="ha_config_set_scene",
                when=[Predicate(path="args.scene_id", op="eq", value="bedroom")],
            ),
            Rule(
                tool_name="*",
                when=[
                    Predicate(
                        path="args.operations.*.entity_id", op="regex", value="^lock"
                    )
                ],
            ),
            Rule(
                tool_name="ha_config_set_scene",
                when=[Predicate(path="args.config.name", op="eq", value="Bedroom")],
            ),
            Rule(
                tool_name="ha_config_set_script",
                when=[Predicate(path="config.entities.*~", op="regex", value="^l")],
            ),
        ],
    )
    def test_a_rule_blind_to_scene_entities_does_not_gate_it(self, rule):
        policy = Policy(rules=[rule])
        assert _capture(policy, capture_devices=["d1"]) == Verdict.ALLOW
