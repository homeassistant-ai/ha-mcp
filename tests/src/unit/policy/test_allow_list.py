"""Allow-list policies (issue #2540): ``rule_effect="allow"``.

A matching rule approves the call and every other call requires approval.
Matching is tightened in this mode so that neither a case variant nor one
listed value among unlisted ones can ride an approval: an op must hold both
case-sensitively and case-insensitively, and for every value a wildcard path
or a list argument yields.
"""

import pytest

from ha_mcp.policy.evaluator import Verdict, evaluate, find_matching_rule
from ha_mcp.policy.model import Policy, Predicate, Rule


def _allow(*rules: Rule) -> Policy:
    return Policy(rule_effect="allow", rules=list(rules))


def _allow_one(path: str, op: str, value, tool: str = "ha_call_service") -> Policy:
    return _allow(
        Rule(tool_name=tool, when=[Predicate(path=path, op=op, value=value)])
    )


MQTT_PUBLISH = Rule(
    tool_name="ha_call_service",
    when=[
        Predicate(path="args.domain", op="eq", value="mqtt"),
        Predicate(path="args.service", op="eq", value="publish"),
        Predicate(path="args.data.topic", op="eq", value="home/bridge"),
    ],
)


def _publish(topic: str) -> dict:
    return {"domain": "mqtt", "service": "publish", "data": {"topic": topic}}


class TestAllowListVerdicts:
    def test_default_effect_is_require_approval(self):
        assert Policy().rule_effect == "require_approval"

    def test_empty_allow_list_gates_every_call(self):
        assert evaluate("ha_get_state", {}, _allow()) == Verdict.REQUIRE_APPROVAL

    def test_matching_rule_approves(self):
        policy = _allow(MQTT_PUBLISH)
        assert (
            evaluate("ha_call_service", _publish("home/bridge"), policy)
            == Verdict.ALLOW
        )

    def test_unmatched_call_to_the_same_tool_needs_approval(self):
        policy = _allow(MQTT_PUBLISH)
        args = {"domain": "lock", "service": "unlock"}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_other_tools_need_approval(self):
        policy = _allow(MQTT_PUBLISH)
        assert evaluate("ha_get_state", {}, policy) == Verdict.REQUIRE_APPROVAL

    def test_bare_rule_approves_every_call_to_its_tool(self):
        policy = _allow(Rule(tool_name="ha_get_state"))
        assert evaluate("ha_get_state", {"entity_id": "x"}, policy) == Verdict.ALLOW

    def test_missing_argument_fails_closed(self):
        policy = _allow(MQTT_PUBLISH)
        args = {"domain": "mqtt", "service": "publish"}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_ws_command_is_not_covered_by_a_domain_service_approval(self):
        policy = _allow(MQTT_PUBLISH)
        args = {"ws_command": "config/entity_registry/remove"}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_no_gating_rule_is_reported_for_an_allow_list(self):
        # Middleware names the rule that gated a call; under an allow list a
        # call is gated because nothing approved it, so there is none.
        policy = _allow(MQTT_PUBLISH)
        assert find_matching_rule("ha_call_service", _publish("home/bridge"), policy) is None


class TestAllowListIsCaseSensitive:
    """No case variant may satisfy an approving predicate."""

    @pytest.mark.parametrize(
        "op,value,arg",
        [
            ("eq", "home/bridge", "Home/Bridge"),
            ("in", ["home/bridge"], "HOME/bridge"),
            ("regex", "^home/bridge$", "Home/Bridge"),
            ("contains", "bridge", "home/BRIDGE"),
        ],
    )
    def test_case_variant_is_not_approved(self, op, value, arg):
        policy = _allow_one("args.data.topic", op, value)
        args = {"data": {"topic": arg}}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "op,value",
        [
            ("neq", "lock"),
            ("not_in", ["lock", "alarm_control_panel"]),
            ("regex", "^(?!lock$)"),
        ],
    )
    def test_negated_op_is_not_satisfied_by_a_case_variant(self, op, value):
        # Home Assistant lower-cases the domain before dispatch, so "LOCK"
        # would run as "lock" -- the one domain this rule excludes.
        policy = _allow_one("args.domain", op, value)
        for domain in ("LOCK", "Lock", "lock"):
            args = {"domain": domain, "service": "unlock"}
            assert (
                evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL
            ), domain
        assert (
            evaluate("ha_call_service", {"domain": "light"}, policy) == Verdict.ALLOW
        )

    def test_negated_selector_domain_is_not_satisfied_by_a_case_variant(self):
        policy = _allow_one("args.selector.domain", "neq", "lock", tool="ha_bulk_control")
        args = {"selector": {"domain": "LOCK"}, "action": "unlock"}
        assert evaluate("ha_bulk_control", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_exact_case_is_approved(self):
        assert (
            evaluate("ha_call_service", _publish("home/bridge"), _allow(MQTT_PUBLISH))
            == Verdict.ALLOW
        )

    def test_require_approval_list_still_ignores_case(self):
        policy = Policy(rules=[MQTT_PUBLISH])
        assert (
            evaluate("ha_call_service", _publish("Home/Bridge"), policy)
            == Verdict.REQUIRE_APPROVAL
        )


class TestAllowListWildcardIsUniversal:
    LIGHTS_ONLY = Rule(
        tool_name="ha_bulk_control",
        when=[
            Predicate(
                path="args.operations.*.entity_id",
                op="in",
                value=["light.a", "light.b"],
            )
        ],
    )

    def _ops(self, *entity_ids: str) -> dict:
        return {"operations": [{"entity_id": e, "action": "on"} for e in entity_ids]}

    def test_every_target_listed_is_approved(self):
        policy = _allow(self.LIGHTS_ONLY)
        args = self._ops("light.a", "light.b")
        assert evaluate("ha_bulk_control", args, policy) == Verdict.ALLOW

    def test_one_unlisted_target_needs_approval(self):
        policy = _allow(self.LIGHTS_ONLY)
        args = self._ops("light.a", "lock.front")
        assert evaluate("ha_bulk_control", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_empty_operations_list_needs_approval(self):
        policy = _allow(self.LIGHTS_ONLY)
        args = {"operations": []}
        assert evaluate("ha_bulk_control", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_require_approval_list_keeps_any_semantics(self):
        gate_locks = Rule(
            tool_name="ha_bulk_control",
            when=[
                Predicate(
                    path="args.operations.*.entity_id", op="in", value=["lock.front"]
                )
            ],
        )
        args = self._ops("light.a", "lock.front")
        assert (
            evaluate("ha_bulk_control", args, Policy(rules=[gate_locks]))
            == Verdict.REQUIRE_APPROVAL
        )


class TestAllowListListArguments:
    """A list at a concrete path counts as its items, each of which must pass."""

    @pytest.mark.parametrize(
        "op,value",
        [
            ("neq", "lock.front_door"),
            ("not_in", ["lock.front_door"]),
            ("contains", "light.a"),
        ],
    )
    def test_list_naming_an_excluded_entity_needs_approval(self, op, value):
        policy = _allow_one("args.data.entity_id", op, value)
        args = {"data": {"entity_id": ["light.a", "lock.front_door"]}}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_list_of_listed_entities_is_approved(self):
        policy = _allow(
            Rule(
                tool_name="ha_call_service",
                when=[
                    Predicate(
                        path="args.data.entity_id", op="in", value=["light.a", "light.b"]
                    )
                ],
            )
        )
        args = {"data": {"entity_id": ["light.a", "light.b"]}}
        assert evaluate("ha_call_service", args, policy) == Verdict.ALLOW

    def test_empty_list_needs_approval(self):
        policy = _allow_one("args.data.entity_id", "neq", "lock.x")
        args = {"data": {"entity_id": []}}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_whole_argument_object_can_be_approved_exactly(self):
        approved = {"domain": "mqtt", "service": "publish", "data": {"topic": "t"}}
        policy = _allow(
            Rule(
                tool_name="ha_call_service",
                when=[Predicate(path="args", op="in", value=[approved])],
            )
        )
        assert evaluate("ha_call_service", approved, policy) == Verdict.ALLOW
        changed = {**approved, "data": {"topic": "T"}}
        assert evaluate("ha_call_service", changed, policy) == Verdict.REQUIRE_APPROVAL


class TestAllowListSplittableStrings:
    """Home Assistant splits entity IDs on commas and strips each one."""

    @pytest.mark.parametrize(
        "entity_id",
        ["light.a,lock.front_door", "light.a, lock.front_door", "lock.front_door ", " x"],
    )
    @pytest.mark.parametrize(
        "op,value",
        [
            ("neq", "lock.front_door"),
            ("not_in", ["lock.front_door"]),
            ("regex", r"^(?!lock\.front_door$)"),
        ],
    )
    def test_splittable_string_needs_approval(self, entity_id, op, value):
        policy = _allow_one("args.entity_id", op, value)
        args = {"domain": "lock", "service": "unlock", "entity_id": entity_id}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "op,value", [("regex", r"^light\."), ("contains", "light."), ("in", ["light.a"])]
    )
    @pytest.mark.parametrize("entity_id", ["light.a,lock.front_door", "light.a "])
    def test_splittable_string_fails_positive_ops_too(self, entity_id, op, value):
        policy = _allow_one("args.entity_id", op, value)
        args = {"entity_id": entity_id}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL
        assert evaluate("ha_call_service", {"entity_id": "light.a"}, policy) == Verdict.ALLOW

    def test_plain_other_entity_is_approved(self):
        policy = _allow_one("args.entity_id", "neq", "lock.x")
        assert (
            evaluate("ha_call_service", {"entity_id": "light.a"}, policy)
            == Verdict.ALLOW
        )


class TestAllowListObjectValues:
    def test_negated_op_does_not_approve_an_object(self):
        # A dict is != any string, which says nothing about its contents.
        policy = _allow_one("args.*", "neq", "lock.front")
        args = {"target": {"entity_id": "lock.front"}}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL


    def test_not_in_does_not_approve_an_object(self):
        policy = _allow_one("args.*", "not_in", ["lock.front"])
        args = {"target": {"entity_id": "lock.front"}}
        assert evaluate("ha_call_service", args, policy) == Verdict.REQUIRE_APPROVAL

    def test_object_is_approved_only_by_exact_eq(self):
        target = {"entity_id": "light.a"}
        policy = _allow_one("args.target", "eq", target)
        assert evaluate("ha_call_service", {"target": target}, policy) == Verdict.ALLOW
        other = {"target": {"entity_id": "lock.front"}}
        assert evaluate("ha_call_service", other, policy) == Verdict.REQUIRE_APPROVAL


class TestAllowListDispatch:
    def test_unknown_effect_fails_closed(self):
        # Validation rejects this value; evaluate() must not treat whatever
        # slips past as the permissive require-approval reading.
        policy = Policy.model_construct(
            rule_effect="Allow", rules=[Rule(tool_name="ha_restart")]
        )
        assert evaluate("ha_get_state", {}, policy) == Verdict.REQUIRE_APPROVAL

    def test_unknown_effect_is_rejected_by_validation(self):
        with pytest.raises(ValueError):
            Policy.model_validate({"rule_effect": "Allow"})


class TestAllowListSelectorCalls:
    """A selector resolves its targets inside the tool, after the gate."""

    SELECTOR_CALL = {"selector": {"domain": "light"}, "action": "on"}

    def test_operations_rule_cannot_approve_a_selector_call(self):
        # ``args.*`` reaches the selector dict too, so ``exists`` holds for
        # it; the rule still cannot see the targets the selector resolves to.
        rule = Rule(
            tool_name="ha_bulk_control",
            when=[Predicate(path="args.*", op="exists")],
        )
        assert (
            evaluate("ha_bulk_control", self.SELECTOR_CALL, _allow(rule))
            == Verdict.REQUIRE_APPROVAL
        )

    def test_selector_field_rule_approves_a_selector_call(self):
        rule = Rule(
            tool_name="ha_bulk_control",
            when=[Predicate(path="args.selector.domain", op="eq", value="light")],
        )
        assert (
            evaluate("ha_bulk_control", self.SELECTOR_CALL, _allow(rule))
            == Verdict.ALLOW
        )

    def test_a_later_selector_field_rule_still_approves(self):
        operations_rule = Rule(
            tool_name="ha_bulk_control",
            when=[Predicate(path="args.*", op="exists")],
        )
        selector_rule = Rule(
            tool_name="ha_bulk_control",
            when=[Predicate(path="args.selector.domain", op="eq", value="light")],
        )
        policy = _allow(operations_rule, selector_rule)
        assert evaluate("ha_bulk_control", self.SELECTOR_CALL, policy) == Verdict.ALLOW
