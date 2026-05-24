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

    def test_empty_path_rejected(self):
        with pytest.raises(ValidationError):
            Predicate(path="", op="eq", value="x")

    def test_regex_value_must_be_string(self):
        with pytest.raises(ValidationError, match="op='regex' requires value: str"):
            Predicate(path="args.x", op="regex", value=123)

    def test_regex_value_must_compile(self):
        with pytest.raises(ValidationError, match="Invalid regex"):
            Predicate(path="args.x", op="regex", value="[unclosed")

    def test_regex_valid_compiles(self):
        p = Predicate(path="args.x", op="regex", value=r"^lock\..+")
        assert p.op == "regex"

    def test_in_value_must_be_list(self):
        with pytest.raises(ValidationError, match="op='in' requires value: list"):
            Predicate(path="args.x", op="in", value="not_a_list")

    def test_not_in_value_must_be_list(self):
        with pytest.raises(ValidationError, match="op='not_in' requires value: list"):
            Predicate(path="args.x", op="not_in", value="not_a_list")

    def test_gt_requires_non_none_value(self):
        with pytest.raises(ValidationError, match="op='gt' requires"):
            Predicate(path="args.x", op="gt", value=None)

    def test_lt_requires_non_none_value(self):
        with pytest.raises(ValidationError, match="op='lt' requires"):
            Predicate(path="args.x", op="lt", value=None)

    def test_exists_rejects_value(self):
        # 'exists' is a presence-only check; accepting a value silently
        # would mislead a user into thinking it compares against that
        # value. Reject at construction so the mistake surfaces in the UI.
        with pytest.raises(ValidationError, match="op='exists' must not have a value"):
            Predicate(path="args.x", op="exists", value="anything")


class TestRule:
    def test_minimal_rule(self):
        r = Rule(tool_name="ha_call_service")
        assert r.when == []
        assert r.remember_minutes == 0

    def test_wildcard_tool_name(self):
        Rule(tool_name="*")

    def test_rule_wildcard_accepted(self):
        # Explicit wildcard construction succeeds and round-trips.
        r = Rule(tool_name="*")
        assert r.tool_name == "*"

    def test_rule_rejects_empty_tool_name(self):
        with pytest.raises(ValidationError, match="tool_name must be non-empty"):
            Rule(tool_name="")

    def test_remember_minutes_non_negative(self):
        with pytest.raises(ValidationError):
            Rule(tool_name="ha_x", remember_minutes=-1)


class TestPolicy:
    def test_defaults(self):
        p = Policy()
        assert p.wait_seconds == 60
        assert p.approval_ttl_minutes == 5
        assert p.rules == []

    def test_user_example_from_issue(self):
        """Example from the maintainer: ha_call_service approval when domain is lock or alarm_control_panel."""
        p = Policy(
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
