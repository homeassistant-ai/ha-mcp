import pytest

from ha_mcp.policy.evaluator import (
    Verdict,
    _MISSING,
    evaluate,
    extract_path,
    find_matching_rule,
    match_predicate,
    match_rule,
)
from ha_mcp.policy.model import Policy, Predicate, Rule


# --- extract_path ---
class TestExtractPath:
    def test_args_top_level(self):
        assert extract_path({"domain": "light"}, "args.domain") == "light"

    def test_nested(self):
        assert extract_path({"config": {"alias": "x"}}, "args.config.alias") == "x"

    def test_missing_returns_sentinel(self):
        assert extract_path({}, "args.domain") is _MISSING


# --- match_predicate ---
class TestMatchPredicate:
    @pytest.mark.parametrize("op,value,arg,expected", [
        ("eq", "lock", "lock", True),
        ("eq", "lock", "light", False),
        ("neq", "lock", "light", True),
        ("in", ["lock", "alarm_control_panel"], "lock", True),
        ("in", ["lock"], "light", False),
        ("not_in", ["lock"], "light", True),
        ("regex", r"^lock\..*", "lock.front", True),
        ("regex", r"^lock\..*", "light.kitchen", False),
        ("contains", "lock", "front_door_lock", True),
        ("gt", 5, 10, True),
        ("gt", 5, 3, False),
        ("lt", 5, 3, True),
    ])
    def test_ops(self, op, value, arg, expected):
        p = Predicate(path="args.x", op=op, value=value)
        assert match_predicate(p, {"x": arg}) is expected

    def test_exists_true_when_present(self):
        p = Predicate(path="args.x", op="exists")
        assert match_predicate(p, {"x": "anything"}) is True

    def test_exists_false_when_missing(self):
        p = Predicate(path="args.x", op="exists")
        assert match_predicate(p, {}) is False

    def test_missing_path_never_matches_except_exists(self):
        for op in ["eq", "in", "regex", "contains", "gt"]:
            p = Predicate(path="args.x", op=op, value="anything")
            assert match_predicate(p, {}) is False


# --- match_rule ---
class TestMatchRule:
    def test_empty_when_matches_any_args(self):
        r = Rule(tool_name="ha_call_service")
        assert match_rule(r, "ha_call_service", {}) is True

    def test_tool_name_mismatch(self):
        r = Rule(tool_name="ha_x")
        assert match_rule(r, "ha_y", {}) is False

    def test_wildcard_tool_name(self):
        r = Rule(tool_name="*")
        assert match_rule(r, "anything", {}) is True

    def test_all_predicates_must_match(self):
        r = Rule(tool_name="ha_call_service", when=[
            Predicate(path="args.domain", op="eq", value="lock"),
            Predicate(path="args.service", op="eq", value="unlock"),
        ])
        assert match_rule(r, "ha_call_service",
                          {"domain": "lock", "service": "unlock"}) is True
        assert match_rule(r, "ha_call_service",
                          {"domain": "lock", "service": "lock"}) is False


# --- evaluate ---
class TestEvaluate:
    def test_disabled_policy_allows_everything(self):
        p = Policy(enabled=False, rules=[Rule(tool_name="*")])
        assert evaluate("ha_call_service", {}, p) == Verdict.ALLOW

    def test_no_rules_with_default_allow(self):
        p = Policy(enabled=True, default_action="allow")
        assert evaluate("ha_call_service", {}, p) == Verdict.ALLOW

    def test_no_rules_with_default_require(self):
        p = Policy(enabled=True, default_action="require_approval")
        assert evaluate("ha_call_service", {}, p) == Verdict.REQUIRE_APPROVAL

    def test_rule_match_returns_require(self):
        p = Policy(enabled=True, rules=[
            Rule(tool_name="ha_call_service", when=[
                Predicate(path="args.domain", op="in", value=["lock"])])])
        assert evaluate("ha_call_service", {"domain": "lock"}, p) == \
               Verdict.REQUIRE_APPROVAL
        assert evaluate("ha_call_service", {"domain": "light"}, p) == \
               Verdict.ALLOW

    def test_first_match_wins(self):
        """Rules evaluated in order; caller finds the matching rule's lifetime via find_matching_rule."""
        p = Policy(enabled=True, rules=[
            Rule(tool_name="ha_call_service", remember_minutes=10),
            Rule(tool_name="ha_call_service", remember_minutes=999),
        ])
        first = find_matching_rule("ha_call_service", {}, p)
        assert first is not None
        assert first.remember_minutes == 10
