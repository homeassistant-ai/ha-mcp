"""Merge a test-only logger entry into a seeded ``configuration.yaml``.

The shared seed (``tests/initial_test_state/configuration.yaml``) carries a
top-level ``logger:`` block that raises the ha_mcp_tools component to INFO for
the in-HA LLM-API proof. A test that needs its own logger entry cannot append a
second ``logger:`` key, which is a duplicate mapping key Home Assistant refuses
to load, so it merges the entry under the seed's ``logs:`` mapping instead.
Pure string function, unit-tested in ``tests/src/unit/test_logger_seed.py``.
"""

from __future__ import annotations


def with_probe_logger_config(
    existing: str, component_yaml: str, logger_entry: str
) -> str:
    """Return ``existing`` plus ``component_yaml`` and ``logger_entry``.

    ``logger_entry`` is one indented ``logs:`` line (four spaces, trailing
    newline) and is merged under the seed's existing ``logger:`` block. A seed
    with no ``logger:`` block gets a whole one appended. A ``logger:`` block
    with no ``logs:`` mapping raises: the seed is then in a shape this helper
    cannot merge into, and appending blindly would produce the duplicate key
    this function exists to avoid.
    """
    lines = existing.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("logger:"):
            continue
        for inner in range(index + 1, len(lines)):
            if lines[inner].startswith("  logs:"):
                lines.insert(inner + 1, logger_entry)
                return "".join(lines) + component_yaml
            if lines[inner].strip() and not lines[inner].startswith((" ", "#")):
                break
        raise AssertionError(
            "the seeded `logger:` block has no `logs:` mapping to merge the "
            "probe's logger entry into"
        )
    return existing + component_yaml + "logger:\n  logs:\n" + logger_entry
