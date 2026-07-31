"""Unit tests for the leading-underscore strip helper in util_helpers.

`public_fields` centralises the convention that ha-mcp tool layers
enrich entity / area dicts with internal fields like ``_hidden_by`` /
``_aliases`` so downstream branches can rank without re-querying the
registry, and those fields must not leak through public tool returns
(see the projection path in tools_search.py).

The non-mutating half of the contract is load-bearing: at the call site
the source dict is read again right after the copy
(``apply_hidden_penalty(100, entity.get("_hidden_by"))``), so a
``public_fields`` that stripped the source in place would silently skip
the hidden-entity score penalty while the output still looked clean —
these tests pin the contract.
"""

from ha_mcp.tools.util_helpers import public_fields


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
