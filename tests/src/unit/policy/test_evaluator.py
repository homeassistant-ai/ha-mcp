import pytest

from ha_mcp.policy.evaluator import (
    Verdict,
    evaluate,
    find_matching_rule,
    iter_path_values,
    match_predicate,
    match_rule,
    normalize_stringified_containers,
)
from ha_mcp.policy.model import Policy, Predicate, Rule


# --- iter_path_values ---
class TestIterPathValues:
    def test_args_top_level(self):
        assert list(iter_path_values({"domain": "light"}, "args.domain")) == ["light"]

    def test_nested(self):
        assert list(
            iter_path_values({"config": {"alias": "x"}}, "args.config.alias")
        ) == ["x"]

    def test_missing_returns_empty(self):
        assert list(iter_path_values({}, "args.domain")) == []

    def test_wildcard_yields_all_top_level_values(self):
        assert sorted(
            iter_path_values({"domain": "light", "service": "turn_on"}, "args.*")
        ) == ["light", "turn_on"]

    def test_wildcard_descends_into_lists(self):
        assert list(iter_path_values({"items": [1, 2, 3]}, "args.items.*")) == [
            1,
            2,
            3,
        ]

    def test_wildcard_on_empty_dict_yields_nothing(self):
        assert list(iter_path_values({}, "args.*")) == []

    def test_wildcard_on_scalar_arg_silently_no_ops(self):
        # `args.x.*` against a scalar arg (string/int/None) should yield
        # nothing — predicates targeting a deep path through a non-dict
        # node just don't match, instead of crashing.
        assert list(iter_path_values({"x": "lock"}, "args.x.*")) == []
        assert list(iter_path_values({"x": 42}, "args.x.*")) == []
        assert list(iter_path_values({"x": None}, "args.x.*")) == []


# --- match_predicate ---
class TestMatchPredicate:
    @pytest.mark.parametrize(
        "op,value,arg,expected",
        [
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
        ],
    )
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
        # Use op-appropriate values so the Predicate field_validator doesn't
        # reject at construction; we're testing the matcher's missing-path branch.
        for op, value in [
            ("eq", "anything"),
            ("in", ["anything"]),
            ("regex", "anything"),
            ("contains", "anything"),
            ("gt", 1),
        ]:
            p = Predicate(path="args.x", op=op, value=value)
            assert match_predicate(p, {}) is False

    def test_gt_lt_type_mismatch_returns_false(self):
        # Comparing a str against an int raises TypeError in Python 3;
        # the matcher must degrade to False so a hand-edited policy with
        # the wrong predicate value-type doesn't crash a tool call.
        p_gt = Predicate(path="args.x", op="gt", value=5)
        assert match_predicate(p_gt, {"x": "not-a-number"}) is False
        p_lt = Predicate(path="args.x", op="lt", value=5)
        assert match_predicate(p_lt, {"x": "not-a-number"}) is False


# --- normalize_stringified_containers ---
class TestNormalizeStringifiedContainers:
    def test_top_level_json_object_string_is_parsed(self):
        assert normalize_stringified_containers(
            {"selector": '{"domain": "light"}'}
        ) == {"selector": {"domain": "light"}}

    def test_json_array_string_is_parsed(self):
        assert normalize_stringified_containers({"area_ids": '["salon"]'}) == {
            "area_ids": ["salon"]
        }

    def test_nested_stringified_value_is_also_parsed(self):
        assert normalize_stringified_containers(
            {"selector": {"area_ids": '["salon"]'}}
        ) == {"selector": {"area_ids": ["salon"]}}

    def test_stringified_value_inside_a_list_is_parsed(self):
        assert normalize_stringified_containers(
            {"operations": ['{"entity_id": "light.one"}']}
        ) == {"operations": [{"entity_id": "light.one"}]}

    def test_plain_string_passes_through_unchanged(self):
        assert normalize_stringified_containers({"action": "off"}) == {"action": "off"}

    def test_jinja_template_string_passes_through_unchanged(self):
        template = "{{ states('sensor.x') }}"
        assert normalize_stringified_containers({"template": template}) == {
            "template": template
        }

    def test_malformed_container_like_string_left_alone_not_raised(self):
        """Policy evaluation is not the place to surface a JSON syntax
        error -- that belongs to the tool's own validation, with a properly
        attributed parameter name."""
        malformed = '{"domain": "light"'
        assert normalize_stringified_containers({"selector": malformed}) == {
            "selector": malformed
        }

    def test_non_string_scalars_pass_through_unchanged(self):
        assert normalize_stringified_containers(
            {"validate_first": True, "timeout_seconds": 5, "extra": None}
        ) == {"validate_first": True, "timeout_seconds": 5, "extra": None}

    def test_deeply_nested_input_does_not_crash_with_recursion_error(self):
        """A RecursionError from this function's OWN dict/list recursion
        (not json.loads's, which loads_if_json_container_str already
        guards) must be caught rather than propagate and crash policy
        evaluation -- mirrors loads_if_json_container_str's own
        RecursionError handling by leaving the whole value unrepaired.
        """
        import sys

        nested: object = "leaf"
        for _ in range(sys.getrecursionlimit() + 100):
            nested = {"nested": nested}

        result = normalize_stringified_containers({"selector": nested})

        assert result == {"selector": nested}


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
        r = Rule(
            tool_name="ha_call_service",
            when=[
                Predicate(path="args.domain", op="eq", value="lock"),
                Predicate(path="args.service", op="eq", value="unlock"),
            ],
        )
        assert (
            match_rule(r, "ha_call_service", {"domain": "lock", "service": "unlock"})
            is True
        )
        assert (
            match_rule(r, "ha_call_service", {"domain": "lock", "service": "lock"})
            is False
        )


# --- evaluate ---
class TestEvaluate:
    def test_no_rules_returns_allow(self):
        p = Policy()
        assert evaluate("ha_call_service", {}, p) == Verdict.ALLOW

    def test_rule_match_returns_require(self):
        p = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.domain", op="in", value=["lock"])],
                )
            ],
        )
        assert (
            evaluate("ha_call_service", {"domain": "lock"}, p)
            == Verdict.REQUIRE_APPROVAL
        )
        assert evaluate("ha_call_service", {"domain": "light"}, p) == Verdict.ALLOW

    def test_first_match_wins(self):
        """Rules evaluated in order; caller finds the matching rule's lifetime via find_matching_rule."""
        p = Policy(
            rules=[
                Rule(tool_name="ha_call_service", remember_minutes=10),
                Rule(tool_name="ha_call_service", remember_minutes=999),
            ],
        )
        first = find_matching_rule("ha_call_service", {}, p)
        assert first is not None
        assert first.remember_minutes == 10

    def test_any_of_multiple_same_tool_rules_gates(self):
        """Each UI 'condition' persists as its own rule; the tool gates if ANY
        of them matches (OR across rules), even when the others don't. This is
        the semantics behind the ALL->ANY policy-editor change (PR #1993)."""
        p = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.domain", op="eq", value="lock")],
                ),
                Rule(
                    tool_name="ha_call_service",
                    when=[
                        Predicate(
                            path="args.domain", op="eq", value="alarm_control_panel"
                        )
                    ],
                ),
            ],
        )
        assert (
            evaluate("ha_call_service", {"domain": "lock"}, p)
            == Verdict.REQUIRE_APPROVAL
        )
        assert (
            evaluate("ha_call_service", {"domain": "alarm_control_panel"}, p)
            == Verdict.REQUIRE_APPROVAL
        )
        # Neither condition matches -> allowed.
        assert evaluate("ha_call_service", {"domain": "light"}, p) == Verdict.ALLOW


# --- case-insensitive string comparison ---
class TestCaseInsensitive:
    @pytest.mark.parametrize(
        "op,value,arg",
        [
            ("eq", "lock", "LOCK"),
            ("eq", "Lock", "lock"),
            ("in", ["lock", "alarm"], "LOCK"),
            ("not_in", ["lock"], "light"),  # still True (case-insensitive miss)
            ("contains", "lock", "FRONT_DOOR_LOCK"),
            ("regex", r"^light\.", "Light.kitchen"),
        ],
    )
    def test_string_ops_ignore_case(self, op, value, arg):
        p = Predicate(path="args.x", op=op, value=value)
        assert match_predicate(p, {"x": arg}) is True

    def test_neq_case_insensitive_same_string_returns_false(self):
        # 'Lock' == 'lock' under CI, so neq should be False.
        p = Predicate(path="args.x", op="neq", value="Lock")
        assert match_predicate(p, {"x": "lock"}) is False

    def test_non_string_types_preserve_natural_equality(self):
        # CI lowering is string-only — int != "1" still.
        p = Predicate(path="args.x", op="eq", value="1")
        assert match_predicate(p, {"x": 1}) is False

    def test_contains_list_membership_ignores_case(self):
        # ``contains`` against a list/tuple/set must mirror ``in`` /
        # ``not_in`` — a rule listing ``"light.kitchen"`` has to fire
        # when the LLM passes ``["Light.Kitchen"]``. Pre-fix this
        # branch was case-sensitive while every other op was CI.
        p = Predicate(path="args.entity_id", op="contains", value="light.kitchen")
        assert match_predicate(p, {"entity_id": ["Light.Kitchen"]}) is True
        assert (
            match_predicate(p, {"entity_id": ("LIGHT.KITCHEN", "other.thing")}) is True
        )
        assert match_predicate(p, {"entity_id": {"foo", "Light.Kitchen"}}) is True

    def test_contains_list_membership_non_string_elements_preserve_equality(self):
        # Mixed-type collections must keep natural equality for the
        # non-string entries — _ci passes non-strings through unchanged.
        p = Predicate(path="args.x", op="contains", value=42)
        assert match_predicate(p, {"x": [1, 42, "three"]}) is True
        assert match_predicate(p, {"x": ["1", "42"]}) is False  # int != "42"


# --- wildcard path semantics (catch-all "any argument matches X") ---
class TestWildcardPredicate:
    def test_wildcard_eq_matches_when_any_arg_equals_value(self):
        p = Predicate(path="args.*", op="eq", value="lock")
        assert match_predicate(p, {"domain": "lock", "service": "unlock"}) is True
        assert match_predicate(p, {"domain": "light", "service": "turn_on"}) is False

    def test_wildcard_in_matches_when_any_arg_is_in_value_list(self):
        p = Predicate(path="args.*", op="in", value=["lock", "alarm"])
        assert match_predicate(p, {"service": "alarm"}) is True
        assert match_predicate(p, {"service": "unlock"}) is False

    def test_wildcard_exists_matches_any_args_present(self):
        p = Predicate(path="args.*", op="exists")
        assert match_predicate(p, {"x": 1}) is True
        assert match_predicate(p, {}) is False

    def test_wildcard_regex_matches_any_string_arg(self):
        p = Predicate(path="args.*", op="regex", value="^light\\.")
        assert match_predicate(p, {"entity_id": "light.bedroom"}) is True
        assert match_predicate(p, {"entity_id": "switch.fan"}) is False

    def test_wildcard_evaluate_end_to_end(self):
        pol = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.*", op="eq", value="lock")],
                ),
            ],
        )
        assert (
            evaluate("ha_call_service", {"domain": "lock", "service": "unlock"}, pol)
            == Verdict.REQUIRE_APPROVAL
        )
        assert evaluate("ha_call_service", {"domain": "light"}, pol) == Verdict.ALLOW


# --- ws_command escape hatch fail-safe ---
class TestWsCommandEscapeHatch:
    """``ha_call_service`` exposes a raw WebSocket ``ws_command`` escape hatch
    with no ``domain``/``service`` args, so domain/service-keyed rules can't
    match it. ``evaluate`` closes that gap: if the policy has ANY rule that
    applies to ``ha_call_service`` -- scoped to it by name OR a wildcard ``*``
    rule -- but none matched normally, an unmatched ws_command call still
    requires approval (fail-safe) rather than sneaking through the fail-open
    default."""

    def test_bypass_closed_by_non_matching_domain_rule(self):
        p = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.domain", op="eq", value="light")],
                )
            ],
        )
        assert (
            evaluate("ha_call_service", {"ws_command": "repairs/ignore_issue"}, p)
            == Verdict.REQUIRE_APPROVAL
        )

    def test_fail_open_preserved_with_no_rules(self):
        p = Policy()
        assert (
            evaluate("ha_call_service", {"ws_command": "repairs/ignore_issue"}, p)
            == Verdict.ALLOW
        )

    def test_only_other_tool_rule_does_not_force_gate(self):
        p = Policy(
            rules=[Rule(tool_name="ha_config_set_dashboard")],
        )
        assert (
            evaluate("ha_call_service", {"ws_command": "repairs/ignore_issue"}, p)
            == Verdict.ALLOW
        )

    def test_empty_when_ha_call_service_rule_gates_ws_command(self):
        p = Policy(
            rules=[Rule(tool_name="ha_call_service", when=[])],
        )
        assert (
            evaluate("ha_call_service", {"ws_command": "repairs/ignore_issue"}, p)
            == Verdict.REQUIRE_APPROVAL
        )

    def test_explicit_ws_command_rule_matches_normally(self):
        p = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.ws_command", op="exists")],
                )
            ],
        )
        assert (
            evaluate("ha_call_service", {"ws_command": "repairs/ignore_issue"}, p)
            == Verdict.REQUIRE_APPROVAL
        )

    def test_normal_service_call_unaffected_non_matching(self):
        p = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.domain", op="eq", value="light")],
                )
            ],
        )
        assert (
            evaluate(
                "ha_call_service",
                {"domain": "cover", "service": "open_cover", "entity_id": "cover.x"},
                p,
            )
            == Verdict.ALLOW
        )

    def test_normal_service_call_unaffected_matching(self):
        p = Policy(
            rules=[
                Rule(
                    tool_name="ha_call_service",
                    when=[Predicate(path="args.domain", op="eq", value="light")],
                )
            ],
        )
        assert (
            evaluate("ha_call_service", {"domain": "light", "service": "turn_on"}, p)
            == Verdict.REQUIRE_APPROVAL
        )

    def test_wildcard_tool_rule_force_gates_ws_command(self):
        # A "*" rule applies to ha_call_service (match_rule treats "*" as any
        # tool), so an operator gating broadly signals MORE caution than a
        # service-scoped one. A domain/entity predicate a domain-less ws_command
        # can't satisfy would otherwise fall through find_matching_rule AND the
        # fail-safe, landing on ALLOW — the escape hatch dodging a broad gate.
        # The fail-safe therefore counts "*" rules too, erring toward blocking
        # (require-approval) rather than silently allowing.
        p = Policy(
            rules=[
                Rule(
                    tool_name="*",
                    when=[
                        Predicate(path="args.entity_id", op="eq", value="lock.front")
                    ],
                )
            ],
        )
        assert (
            evaluate("ha_call_service", {"ws_command": "repairs/ignore_issue"}, p)
            == Verdict.REQUIRE_APPROVAL
        )


class TestBulkSelectorFailSafe:
    """Structural selectors cannot bypass operation-shaped approval rules."""

    def test_operations_rule_force_gates_selector(self):
        """A selector is gated when its eventual leaves cannot be inspected yet."""
        policy = Policy(
            rules=[
                Rule(
                    tool_name="ha_bulk_control",
                    when=[
                        Predicate(
                            path="args.operations.*.entity_id",
                            op="regex",
                            value=r"^lock\.",
                        )
                    ],
                )
            ]
        )

        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "light", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )

    def test_wildcard_operations_rule_force_gates_selector(self):
        """A wildcard path that reaches operations rows is not a literal-prefix dodge.

        ``args.*.*.entity_id`` fans out over every top-level key (including
        ``operations``) and every item at the next level, so it matches
        ``operations[i].entity_id`` exactly like ``args.operations.*.entity_id``
        does — but as a bare string it doesn't start with "args.operations".
        The fail-safe must recognize the wildcard can reach unresolved
        operation rows, not just the literal prefix, or a selector call could
        bypass approval a rule like this would have required.
        """
        policy = Policy(
            rules=[
                Rule(
                    tool_name="ha_bulk_control",
                    when=[
                        Predicate(
                            path="args.*.*.entity_id",
                            op="regex",
                            value=r"^lock\.",
                        )
                    ],
                )
            ]
        )

        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "light", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )
        # The same wildcard rule keeps its normal, precise semantics for the
        # operations-mode calls it was actually written to inspect.
        assert (
            evaluate(
                "ha_bulk_control",
                {"operations": [{"entity_id": "light.sofa", "action": "off"}]},
                policy,
            )
            == Verdict.ALLOW
        )
        assert (
            evaluate(
                "ha_bulk_control",
                {"operations": [{"entity_id": "lock.front", "action": "lock"}]},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )

    def test_single_wildcard_to_dict_field_stays_conditional(self):
        """A single-wildcard path landing on a dict field is not operations-sensitive.

        ``args.*.domain`` CAN yield ``args.selector.domain`` (``selector`` is
        a dict), but it can never yield anything from ``args.operations``
        (a list) — ``walk()`` only descends into a literal segment like
        ``domain`` when the current node is a dict. Treating every leading
        wildcard as operations-sensitive (the bug the previous fix
        introduced) would force-gate a "light" selector call for a rule that
        was written to conditionally match only ``selector.domain == "lock"``
        and, after ``find_matching_rule`` correctly finds no match, has
        nothing left to reach in ``args.operations`` either.
        """
        policy = Policy(
            rules=[
                Rule(
                    tool_name="ha_bulk_control",
                    when=[Predicate(path="args.*.domain", op="eq", value="lock")],
                )
            ]
        )

        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "light", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.ALLOW
        )
        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "lock", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )

    def test_stringified_selector_argument_would_bypass_rules_pre_normalization(self):
        """Documents the exact bypass a raw ``evaluate()`` call is vulnerable to.

        This is what ``evaluate()`` sees BEFORE ``PolicyMiddleware`` applies
        ``normalize_stringified_containers`` (see test_middleware.py's
        end-to-end coverage) -- a client that sends ``selector`` as a JSON
        string (Claude Desktop stdio does this; see
        ``tools/util_helpers.py``'s ``JSON_STRING_COERCION``) makes
        ``args.selector.domain`` yield nothing, so a rule scoped to
        ``selector.domain == "lock"`` never matches even for a lock
        selector. This test pins that ``evaluate()`` itself has no
        opinion on wire shape -- normalization is the middleware's job,
        not this pure function's.
        """
        policy = Policy(
            rules=[
                Rule(
                    tool_name="ha_bulk_control",
                    when=[
                        Predicate(path="args.selector.domain", op="eq", value="lock")
                    ],
                )
            ]
        )
        stringified_args = {"selector": '{"domain": "lock", "area_ids": ["entry"]}'}

        assert evaluate("ha_bulk_control", stringified_args, policy) == Verdict.ALLOW

    def test_selector_remains_allowed_without_applicable_rules(self):
        """Unrelated or absent rules preserve the policy engine's allow default."""
        selector_args = {"selector": {"domain": "light", "area_ids": ["salon"]}}

        assert evaluate("ha_bulk_control", selector_args, Policy()) == Verdict.ALLOW
        assert (
            evaluate(
                "ha_bulk_control",
                selector_args,
                Policy(rules=[Rule(tool_name="ha_config_set_dashboard")]),
            )
            == Verdict.ALLOW
        )

    def test_operations_calls_keep_normal_predicate_semantics(self):
        """The selector fail-safe does not broaden ordinary operations calls."""
        policy = Policy(
            rules=[
                Rule(
                    tool_name="ha_bulk_control",
                    when=[
                        Predicate(
                            path="args.operations.*.entity_id",
                            op="regex",
                            value=r"^lock\.",
                        )
                    ],
                )
            ]
        )

        assert (
            evaluate(
                "ha_bulk_control",
                {"operations": [{"entity_id": "light.sofa", "action": "off"}]},
                policy,
            )
            == Verdict.ALLOW
        )
        assert (
            evaluate(
                "ha_bulk_control",
                {"operations": [{"entity_id": "lock.front", "action": "lock"}]},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )

    def test_wildcard_tool_rule_force_gates_selector(self):
        """A "*" rule counts too, not just one scoped to ha_bulk_control by name.

        ``match_rule`` treats ``tool_name="*"`` as applying to every tool, so
        an operator-wide rule that reaches unresolved operation rows (e.g.
        ``args.operations.*.entity_id``) signals MORE caution than a
        service-scoped one, not less. Every other test in this class uses
        ``tool_name="ha_bulk_control"`` explicitly, leaving the fail-safe's
        own ``rule.tool_name in ("ha_bulk_control", "*")`` "*" arm unexercised
        -- a regression there (e.g. dropping the "*" case) would let a
        selector call bypass a broad operator-wide gate silently.
        """
        policy = Policy(
            rules=[
                Rule(
                    tool_name="*",
                    when=[
                        Predicate(
                            path="args.operations.*.entity_id",
                            op="regex",
                            value=r"^lock\.",
                        )
                    ],
                )
            ]
        )

        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "light", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )

    def test_nonmatching_selector_only_rule_stays_allowed(self):
        """A rule fully expressed over selector fields keeps its condition.

        The fail-safe must only widen rules that depend on unresolved
        ``args.operations`` data. A rule scoped to
        ``args.selector.domain == "lock"`` already got a precise match
        attempt in ``find_matching_rule``; a "light" selector call must not
        be swept into approval just because a same-named rule exists.
        """
        policy = Policy(
            rules=[
                Rule(
                    tool_name="ha_bulk_control",
                    when=[
                        Predicate(path="args.selector.domain", op="eq", value="lock")
                    ],
                )
            ]
        )

        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "light", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.ALLOW
        )
        assert (
            evaluate(
                "ha_bulk_control",
                {"selector": {"domain": "lock", "area_ids": ["salon"]}},
                policy,
            )
            == Verdict.REQUIRE_APPROVAL
        )
