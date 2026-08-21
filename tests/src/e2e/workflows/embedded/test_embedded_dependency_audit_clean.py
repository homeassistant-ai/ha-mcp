"""The #2239 dependency audit must stay silent on a healthy install.

``EmbeddedServerManager._async_warn_on_dependency_conflicts`` runs on EVERY
bring-up and logs a WARNING whenever the audit finds an unsatisfied
requirement reachable from the ha-mcp distribution. That makes its
false-positive rate a user-facing property: a warning that fires on a perfectly
normal install trains people to ignore the one that matters.

Only a real install can answer that. The unit tier audits synthetic metadata
trees, which prove the walk's rules but say nothing about the tree pip actually
resolves inside a Home Assistant image — where the resolution ran under HA's
own constraints file, where some of ha-mcp's transitive dependencies are
packages HA governs and the install deliberately leaves alone (#2135/#2146),
and where marker- and extra-scoped requirements are real rather than
hand-written. This test runs the shipped audit, unmodified, against exactly
that tree.

The embedded lane's session container is the only one that has it: the plain
container lane runs the server inside the pytest process and never installs
ha-mcp into the HA image at all. Gated by the ``embedded_only`` collection
marker (like its neighbour ``test_embedded_no_stomp.py``), so the mass-skip
ceilings account for the skip on every other lane.
"""

from __future__ import annotations

import json

import pytest

_AUDIT_PROBE = """
import json
import sys
from importlib.metadata import PackageNotFoundError, version

sys.path.insert(0, "/config")
from custom_components.ha_mcp_tools.dependency_diagnostics import (
    audit_dependency_graph,
)

root = None
root_version = None
for candidate in ("ha-mcp", "ha-mcp-dev"):
    try:
        root_version = version(candidate)
    except PackageNotFoundError:
        continue
    root = candidate
    break

violations = []
if root is not None:
    violations = [
        {
            "package": v.package,
            "installed": v.installed,
            "requirement": v.requirement,
            "required_by": v.required_by,
        }
        for v in audit_dependency_graph(root)
    ]

print(
    "AUDIT_JSON "
    + json.dumps(
        {"root": root, "root_version": root_version, "violations": violations}
    )
)
"""

pytestmark = pytest.mark.embedded_only


def _run_audit_probe(container) -> dict:
    """Run the shipped audit inside the live container; return its report."""
    result = container.get_wrapped_container().exec_run(["python3", "-c", _AUDIT_PROBE])
    output = (result.output or b"").decode("utf-8", "replace")
    assert result.exit_code == 0, (
        f"the in-container dependency audit exited {result.exit_code}. The "
        "audit must be total — it runs inside an error handler, where a "
        f"traceback replaces the failure it was meant to explain:\n{output}"
    )
    marker = "AUDIT_JSON "
    lines = [line for line in output.splitlines() if line.startswith(marker)]
    assert lines, f"the probe printed no report:\n{output}"
    return json.loads(lines[-1][len(marker) :])


def test_dependency_audit_finds_nothing_on_a_healthy_embedded_install(
    ha_container_with_fresh_config,
):
    info = ha_container_with_fresh_config
    report = _run_audit_probe(info["container"])

    # Without an installed root the audit reports that root as its only
    # violation, and the emptiness check below would prove nothing.
    assert report["root"] is not None, (
        "neither ha-mcp nor ha-mcp-dev has installed metadata in the embedded "
        "container, so the audit had no graph to walk — the lane's bring-up "
        "did not install the server at all"
    )

    assert report["violations"] == [], (
        "the dependency audit reports unsatisfied requirements on a HEALTHY "
        f"embedded install of {report['root']} {report['root_version']}, so "
        "every bring-up on this image logs the #2239 conflict warning and the "
        "signal is worthless when a real conflict appears. Either the audit "
        "over-reports (marker/extra handling, or a package Home Assistant "
        "governs that our install deliberately does not move — see "
        "test_embedded_no_stomp.py) or the dependency tree really is "
        f"inconsistent and needs fixing in pyproject.toml:\n"
        + "\n".join(
            f"  - installed {violation['package']} {violation['installed']} "
            f"does not satisfy {violation['requirement']!r} "
            f"required by {violation['required_by']}"
            for violation in report["violations"]
        )
    )
