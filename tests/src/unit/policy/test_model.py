"""What the policy models refuse before a policy is stored or enforced."""

import pytest
from pydantic import ValidationError

from ha_mcp.policy.model import Policy, Predicate, Rule


class TestPredicate:
    def test_a_stored_condition_with_an_unknown_operator_does_not_load(self):
        # The stored file is untyped JSON, so the operator set is enforced on
        # load, not by the type checker.
        stored = {
            "rules": [
                {
                    "tool_name": "ha_x",
                    "when": [{"path": "args.x", "op": "unknown", "value": 1}],
                }
            ]
        }
        with pytest.raises(ValidationError):
            Policy.model_validate(stored)

    def test_a_condition_on_no_argument_is_refused(self):
        with pytest.raises(ValidationError, match="path must be non-empty"):
            Predicate(path="", op="eq", value="x")

    def test_a_regex_condition_needs_a_pattern_string(self):
        with pytest.raises(ValidationError, match="op='regex' requires value: str"):
            Predicate(path="args.x", op="regex", value=123)

    def test_a_regex_that_does_not_compile_is_refused(self):
        with pytest.raises(ValidationError, match="Invalid regex"):
            Predicate(path="args.x", op="regex", value="[unclosed")

    def test_a_regex_that_compiles_is_accepted(self):
        assert (
            Predicate(path="args.x", op="regex", value=r"^lock\..+").value
            == r"^lock\..+"
        )

    @pytest.mark.parametrize("op", ["in", "not_in"])
    def test_a_membership_condition_needs_a_list(self, op):
        with pytest.raises(ValidationError, match=f"op='{op}' requires value: list"):
            Predicate(path="args.x", op=op, value="not_a_list")

    def test_a_membership_condition_with_a_list_is_accepted(self):
        p = Predicate(
            path="args.domain", op="in", value=["lock", "alarm_control_panel"]
        )
        assert p.value == ["lock", "alarm_control_panel"]

    @pytest.mark.parametrize("op", ["in", "not_in", "regex", "gt", "lt"])
    def test_an_omitted_operand_is_refused_like_an_explicit_null(self, op):
        # An omitted value used to skip validation, serialize as null and
        # then fail the next load of the stored file.
        with pytest.raises(ValidationError, match=f"op='{op}' requires"):
            Predicate(path="args.x", op=op)

    def test_a_presence_check_needs_no_value(self):
        assert Predicate(path="args.entity_id", op="exists").value is None

    def test_a_presence_check_with_a_value_is_refused(self):
        # 'exists' is a presence-only check; accepting a value silently
        # would mislead a user into thinking it compares against that
        # value. Reject at construction so the mistake surfaces in the UI.
        with pytest.raises(ValidationError, match="op='exists' must not have a value"):
            Predicate(path="args.x", op="exists", value="anything")


class TestRule:
    def test_a_rule_without_conditions_matches_every_call_and_remembers_nothing(self):
        r = Rule(tool_name="ha_call_service")
        assert r.when == []
        assert r.remember_minutes == 0

    def test_a_wildcard_rule_is_accepted(self):
        assert Rule(tool_name="*").tool_name == "*"

    def test_a_rule_naming_no_tool_is_refused(self):
        with pytest.raises(ValidationError, match="tool_name must be non-empty"):
            Rule(tool_name="")

    def test_a_negative_remember_window_is_refused(self):
        with pytest.raises(ValidationError, match="remember_minutes"):
            Rule(tool_name="ha_x", remember_minutes=-1)


class TestPolicy:
    def test_an_empty_policy_gates_nothing(self):
        p = Policy()
        assert p.rules == []
        assert p.rule_effect == "require_approval"

    def test_a_wait_that_outlasts_the_approval_is_refused(self):
        # The queue would evict the entry mid-wait and every retry would
        # issue a fresh pending approval.
        with pytest.raises(ValidationError, match="must be less than"):
            Policy(wait_seconds=60, approval_ttl_minutes=1)
