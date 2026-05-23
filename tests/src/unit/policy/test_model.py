import pytest
from pydantic import ValidationError

from ha_mcp.policy.model import Policy, Predicate, Rule


class TestPredicate:
    def test_eq_with_scalar_value(self):
        p = Predicate(path="args.domain", op="eq", value="lock")
        assert p.path == "args.domain"

    def test_in_requires_list_value(self):
        Predicate(path="args.domain", op="in", value=["lock", "alarm_control_panel"])

    def test_exists_value_optional(self):
        p = Predicate(path="args.entity_id", op="exists")
        assert p.value is None

    def test_unknown_op_rejected(self):
        with pytest.raises(ValidationError):
            Predicate(path="args.x", op="unknown", value=1)


class TestRule:
    def test_minimal_rule(self):
        r = Rule(tool_name="ha_call_service")
        assert r.when == []
        assert r.remember_minutes == 0

    def test_wildcard_tool_name(self):
        Rule(tool_name="*")

    def test_remember_minutes_non_negative(self):
        with pytest.raises(ValidationError):
            Rule(tool_name="ha_x", remember_minutes=-1)


class TestPolicy:
    def test_defaults(self):
        p = Policy()
        assert p.enabled is False
        assert p.default_action == "allow"
        assert p.wait_seconds == 60
        assert p.approval_ttl_minutes == 5
        assert p.rules == []

    def test_invalid_default_action(self):
        with pytest.raises(ValidationError):
            Policy(default_action="maybe")

    def test_user_example_from_issue(self):
        """Example from the maintainer: ha_call_service approval when domain is lock or alarm_control_panel."""
        p = Policy(
            enabled=True,
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[
                        Predicate(
                            path="args.domain",
                            op="in",
                            value=["lock", "alarm_control_panel"],
                        )
                    ],
                ),
            ],
        )
        assert p.rules[0].when[0].value == ["lock", "alarm_control_panel"]
