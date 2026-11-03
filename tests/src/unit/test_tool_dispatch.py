"""Call-proxy envelope coercion shared by the read-only and visibility middleware."""

from ha_mcp.tool_dispatch import coerce_proxy_arguments


def test_deeply_nested_arguments_string_is_a_malformed_envelope_not_a_crash():
    depth = 200_000
    nested = '{"a": ' + "[" * depth + "]" * depth + "}"

    assert coerce_proxy_arguments(nested) is None
