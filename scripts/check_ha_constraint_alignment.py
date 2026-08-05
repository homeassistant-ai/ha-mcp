"""Check ha-mcp's direct dependencies stay aligned with HA core's own pins.

Why (issues #2135/#2146): the in-process embedded install pip-installs ha-mcp
into Home Assistant's own site-packages. A dependency spec that forces pip to
replace a package HA already ships opens a torn-install window — pip's
uninstall-then-extract is not atomic, and an interruption leaves files from
two releases in one tree (for ``websockets`` that killed every WS connect
with ImportError). The cure is alignment: where HA speaks, we must agree.

Two rules, applied to every ``[project.dependencies]`` entry whose canonical
name appears in ``homeassistant/package_constraints.txt`` of the HA version
under test (the same version the e2e lanes run against — renovate keeps it
current, so drift on either side fails the very PR that introduces it):

1. HA pins the package exactly (``==V``): our specifier must admit ``V``.
   The embedded install then reuses HA's copy — or fails loudly at resolve
   time, never silently. Rules out an ha-mcp exact pin drifting from HA's.
2. HA constrains it loosely (floor/range): our specifier must NOT be a lone
   exact pin — an exact pin above whatever the image shipped is precisely
   the forced in-place replacement that tears packages (websockets, #2146).

Dependencies HA does not constrain at all are not managed here — renovate
keeps them current, and the embedded-lane no-stomp guard
(tests/src/e2e/workflows/embedded/test_embedded_no_stomp.py) still catches a
collision with an image-shipped transitive package empirically.

Usage:
    python scripts/check_ha_constraint_alignment.py --ha-version 2026.7.4
    python scripts/check_ha_constraint_alignment.py --constraints-file c.txt
"""

from __future__ import annotations

import argparse
import sys
import tomllib
import urllib.request
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONSTRAINTS_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/"
    "{version}/homeassistant/package_constraints.txt"
)


def parse_requirement_lines(text: str) -> dict[str, SpecifierSet]:
    """Parse requirement/constraint lines into {canonical_name: specifier_set}.

    Comments, blank lines, and pip directives are skipped; so is any line
    ``packaging`` cannot parse as a requirement (the constraints file also
    carries a few pip-internal knobs).
    """
    constraints: dict[str, SpecifierSet] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        constraints[canonicalize_name(requirement.name)] = requirement.specifier
    return constraints


def direct_dependencies(pyproject_text: str) -> list[Requirement]:
    """Return ``[project.dependencies]`` parsed as packaging Requirements."""
    project = tomllib.loads(pyproject_text)["project"]
    return [Requirement(dep) for dep in project["dependencies"]]


def _lone_exact_pin(specifier_set: SpecifierSet) -> str | None:
    """Return the pinned version when the set is a single ``==`` clause."""
    specifiers = list(specifier_set)
    if len(specifiers) == 1 and specifiers[0].operator == "==":
        return specifiers[0].version
    return None


def check_alignment(
    dependencies: list[Requirement], ha_constraints: dict[str, SpecifierSet]
) -> list[str]:
    """Return one violation message per dependency breaking the two rules."""
    violations: list[str] = []
    for dependency in dependencies:
        name = canonicalize_name(dependency.name)
        ha_specifier = ha_constraints.get(name)
        if ha_specifier is None:
            continue
        ha_pin = _lone_exact_pin(ha_specifier)
        if ha_pin is not None:
            if not dependency.specifier.contains(ha_pin, prereleases=True):
                violations.append(
                    f"{name}: HA pins {name}=={ha_pin} but ha-mcp requires "
                    f"'{dependency.specifier}', which excludes it. The "
                    "embedded install cannot resolve under HA's constraints "
                    "— realign the ha-mcp spec to admit HA's pin."
                )
        elif _lone_exact_pin(dependency.specifier) is not None:
            violations.append(
                f"{name}: HA constrains it loosely ('{ha_specifier}') but "
                f"ha-mcp pins '{dependency.specifier}' exactly. An exact pin "
                "over a loose HA constraint forces pip to replace the "
                "image-shipped copy in place — the #2135/#2146 torn-install "
                "window. Use a range that admits HA's shipped versions."
            )
    return violations


def _fetch_constraints(ha_version: str) -> str:
    url = _CONSTRAINTS_URL.format(version=ha_version)
    with urllib.request.urlopen(url, timeout=30) as response:
        payload: bytes = response.read()
    return payload.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--ha-version",
        help="HA core version whose package_constraints.txt to check against "
        "(fetched from GitHub), e.g. 2026.7.4",
    )
    source.add_argument(
        "--constraints-file",
        type=Path,
        help="Local package_constraints.txt to check against (offline)",
    )
    args = parser.parse_args(argv)

    if args.constraints_file is not None:
        constraints_text = args.constraints_file.read_text(encoding="utf-8")
        origin = str(args.constraints_file)
    else:
        constraints_text = _fetch_constraints(args.ha_version)
        origin = f"HA core {args.ha_version}"

    ha_constraints = parse_requirement_lines(constraints_text)
    if not ha_constraints:
        print(f"ERROR: no constraints parsed from {origin}", file=sys.stderr)
        return 2

    dependencies = direct_dependencies(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    shared = [
        dep for dep in dependencies if canonicalize_name(dep.name) in ha_constraints
    ]
    print(
        f"Checked {len(dependencies)} direct dependencies against {origin}; "
        f"{len(shared)} overlap HA's constraints:"
    )
    for dep in shared:
        name = canonicalize_name(dep.name)
        print(f"  {name}: ha-mcp '{dep.specifier}' vs HA '{ha_constraints[name]}'")

    violations = check_alignment(dependencies, ha_constraints)
    for violation in violations:
        print(f"VIOLATION: {violation}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
