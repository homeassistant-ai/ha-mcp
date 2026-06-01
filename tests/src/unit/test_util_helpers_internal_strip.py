"""Unit tests for the leading-underscore strip helpers in util_helpers.

These two helpers (`strip_internal_fields` and `public_fields`)
centralise the convention that ha-mcp tool layers enrich entity / area
dicts with internal fields like ``_hidden_by`` / ``_aliases`` so
downstream branches can rank without re-querying the registry, and
those fields must not leak through public tool returns.

A regression that, e.g., stops recursing into nested lists or mishandles
non-string keys would silently surface internal fields through every
public tool that touches them — these tests pin the contract.
"""
from ha_mcp.tools.util_helpers import public_fields, strip_internal_fields


class TestStripInternalFields:
    """Locks down `strip_internal_fields` mutate-in-place contract."""

    def test_removes_top_level_underscore_keys(self):
        d = {"entity_id": "light.x", "_hidden_by": "user", "score": 100}
        result = strip_internal_fields(d)
        assert result is d, "should mutate and return same reference"
        assert d == {"entity_id": "light.x", "score": 100}

    def test_recurses_into_nested_dicts(self):
        d = {
            "outer": "v",
            "nested": {
                "entity_id": "x",
                "_hidden_by": "integration",
                "_aliases": ["a"],
            },
        }
        strip_internal_fields(d)
        assert d == {"outer": "v", "nested": {"entity_id": "x"}}

    def test_recurses_into_lists_of_dicts(self):
        d = {
            "results": [
                {"entity_id": "a", "_hidden_by": "user"},
                {"entity_id": "b", "_aliases": []},
            ]
        }
        strip_internal_fields(d)
        assert d == {"results": [{"entity_id": "a"}, {"entity_id": "b"}]}

    def test_does_not_remove_non_string_keys(self):
        # int keys can never start with "_" — they must pass through
        # untouched, not crash the helper.
        d = {1: "one", 2: "two", "_hidden_by": "x"}
        strip_internal_fields(d)
        assert d == {1: "one", 2: "two"}

    def test_handles_non_dict_non_list_input(self):
        # A bare string / int / None should pass through unchanged.
        assert strip_internal_fields("hello") == "hello"
        assert strip_internal_fields(42) == 42
        assert strip_internal_fields(None) is None

    def test_cycle_guard_prevents_recursion_error(self):
        # JSON payloads can't be cyclic, but the helper is now a public
        # utility — defend against a future caller that constructs one.
        a: dict = {"entity_id": "x"}
        b: dict = {"_hidden_by": "user", "back": a}
        a["nested"] = b
        # Without the cycle guard this would RecursionError.
        strip_internal_fields(a)
        assert "_hidden_by" not in b
        assert a["entity_id"] == "x"

    def test_deep_underscore_strip_in_double_nested_list(self):
        d = {
            "areas": [
                {
                    "entities": [
                        {"entity_id": "a", "_hidden_by": "user"},
                        {"entity_id": "b"},
                    ]
                }
            ]
        }
        strip_internal_fields(d)
        assert d["areas"][0]["entities"][0] == {"entity_id": "a"}

    def test_only_string_underscore_prefix_stripped(self):
        d = {"_hidden_by": "user", "_": "x", "no_underscore_prefix": "y"}
        strip_internal_fields(d)
        # "_" alone (just an underscore) starts with "_" and is stripped.
        assert d == {"no_underscore_prefix": "y"}


class TestPublicFields:
    """Locks down `public_fields` non-mutating shallow-copy contract."""

    def test_returns_new_dict(self):
        d = {"entity_id": "x", "_hidden_by": "user"}
        result = public_fields(d)
        assert result is not d, "must return new dict"

    def test_does_not_mutate_source(self):
        d = {"entity_id": "x", "_hidden_by": "user"}
        public_fields(d)
        # Source must still have the underscore key.
        assert "_hidden_by" in d

    def test_strips_underscore_keys(self):
        d = {"a": 1, "_b": 2, "c": 3, "_aliases": ["x"]}
        assert public_fields(d) == {"a": 1, "c": 3}

    def test_shallow_only_list_values_shared(self):
        # Documents the shallow-copy contract: list/dict values are
        # shared, so a downstream mutation of the value would affect
        # the source.
        shared_list = ["a", "b"]
        d = {"items": shared_list, "_hidden_by": "user"}
        result = public_fields(d)
        assert result["items"] is shared_list

    def test_handles_int_keys(self):
        d = {1: "one", "_hidden_by": "x", "name": "n"}
        # int keys (can't startswith) pass through untouched.
        assert public_fields(d) == {1: "one", "name": "n"}

    def test_empty_dict(self):
        assert public_fields({}) == {}

    def test_no_underscore_keys(self):
        d = {"a": 1, "b": 2}
        result = public_fields(d)
        assert result == d
        assert result is not d  # still a copy
