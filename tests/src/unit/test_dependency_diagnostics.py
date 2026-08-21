"""Unit tests for the dependency-conflict diagnostics (issue #2239).

The incident these are modeled on: a third-party custom integration declaring
``"requirements": ["mcp==1.14.1"]`` had Home Assistant downgrade the shared
``mcp`` package below fastmcp's ``>=1.24.0`` floor at every startup, so the
embedded server died on ``from mcp.types import Icon`` — and the only message
that reached the user was fastmcp's generic "FastMCP server support is not
installed" hint, which names none of that.

``audit_dependency_graph`` is exercised against real ``.dist-info``
directories written under ``tmp_path`` and read back through an explicit
metadata search path, so the walk is tested against ``importlib.metadata``
itself rather than a mock of it. Each scenario gets its own subdirectory:
``importlib.metadata`` caches its directory listing per root path, so reusing
one root across two scans within a test can serve a stale listing.

The module under test imports only the standard library and ``packaging``, but
lives inside the component package, whose ``__init__`` pulls in Home Assistant
— stubbed via ``_embedded_stubs`` like the component's other unit suites.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.dependency_diagnostics import (  # noqa: E402
    DependencyViolation,
    PinningIntegration,
    audit_dependency_graph,
    describe_dependency_failure,
    find_pinning_integrations,
    requirement_forces_conflict,
    root_import_failure,
)

# The real incident's numbers, so the assertions below fail loudly if the
# shapes they pin ever stop describing it.
_PINNED_MCP = "1.14.1"
_FASTMCP_FLOOR = "mcp<2.0,>=1.24.0"
_XIAOZHI_DOMAIN = "ws_mcp_server"
_XIAOZHI_NAME = "MCP Server for Xiaozhi"
_FASTMCP_HINT = (
    "FastMCP server support is not installed. "
    "Install `fastmcp` or `fastmcp-slim[server]`."
)
_ICON_IMPORT_ERROR = "cannot import name 'Icon' from 'mcp.types'"


def _site(tmp_path: Path, scenario: str) -> Path:
    """A fresh metadata search root, isolated from other scans in this test."""
    root = tmp_path / scenario
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_dist(
    site: Path,
    name: str,
    version: str,
    *,
    requires: Sequence[str] = (),
    extras: Sequence[str] = (),
) -> None:
    """Write a minimal but real ``.dist-info`` directory into ``site``."""
    # The wheel naming convention importlib.metadata matches on: the directory
    # stem is split at the FIRST hyphen, so the name part must use underscores
    # or "fastmcp-slim" would be looked up as "fastmcp".
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Provides-Extra: {extra}" for extra in extras]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    (dist_info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_manifest(
    config_dir: Path, domain: str, manifest: str | dict[str, object]
) -> None:
    """Write ``custom_components/<domain>/manifest.json`` (raw text or JSON)."""
    path = config_dir / "custom_components" / domain
    path.mkdir(parents=True, exist_ok=True)
    body = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (path / "manifest.json").write_text(body, encoding="utf-8")


class TestAuditDependencyGraph:
    def test_satisfied_graph_reports_nothing(self, tmp_path):
        site = _site(tmp_path, "satisfied")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["fastmcp-slim>=3.0"])
        _write_dist(site, "fastmcp-slim", "3.4.6", requires=[_FASTMCP_FLOOR])
        _write_dist(site, "mcp", "1.24.0")

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == []

    def test_downgraded_dependency_is_reported(self, tmp_path):
        """The incident: HA reinstalled mcp 1.14.1 under fastmcp's floor."""
        site = _site(tmp_path, "downgraded")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["fastmcp-slim>=3.0"])
        _write_dist(site, "fastmcp-slim", "3.4.6", requires=[_FASTMCP_FLOOR])
        _write_dist(site, "mcp", _PINNED_MCP)

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == [
            DependencyViolation(
                package="mcp",
                installed=_PINNED_MCP,
                requirement=_FASTMCP_FLOOR,
                required_by="fastmcp-slim 3.4.6",
            )
        ]

    def test_missing_dependency_is_reported_without_recursing(self, tmp_path):
        site = _site(tmp_path, "missing")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["fastmcp-slim>=3.0"])
        _write_dist(site, "fastmcp-slim", "3.4.6", requires=[_FASTMCP_FLOOR])

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == [
            DependencyViolation(
                package="mcp",
                installed=None,
                requirement=_FASTMCP_FLOOR,
                required_by="fastmcp-slim 3.4.6",
            )
        ]

    def test_extra_scoped_requirement_checked_when_edge_activates_it(self, tmp_path):
        site = _site(tmp_path, "extra-active")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["fastmcp-slim[server]>=3.0"])
        _write_dist(
            site,
            "fastmcp-slim",
            "3.4.6",
            requires=[f'{_FASTMCP_FLOOR}; extra == "server"'],
            extras=["server"],
        )
        _write_dist(site, "mcp", _PINNED_MCP)

        violations = audit_dependency_graph("ha-mcp", search_path=[str(site)])

        assert [(v.package, v.installed) for v in violations] == [("mcp", _PINNED_MCP)]
        assert violations[0].requirement == f'{_FASTMCP_FLOOR}; extra == "server"'

    def test_extra_scoped_requirement_skipped_without_the_extra(self, tmp_path):
        site = _site(tmp_path, "extra-inactive")
        # Same tree as above, minus the [server] extra on the edge.
        _write_dist(site, "ha-mcp", "7.0.0", requires=["fastmcp-slim>=3.0"])
        _write_dist(
            site,
            "fastmcp-slim",
            "3.4.6",
            requires=[f'{_FASTMCP_FLOOR}; extra == "server"'],
            extras=["server"],
        )
        _write_dist(site, "mcp", _PINNED_MCP)

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == []

    def test_dependency_cycle_terminates_without_duplicates(self, tmp_path):
        site = _site(tmp_path, "cycle")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["cyc-a>=1.0"])
        _write_dist(site, "cyc-a", "1.0", requires=["cyc-b>=1.0"])
        _write_dist(site, "cyc-b", "1.0", requires=["cyc-a>=1.0", "mcp>=1.24.0"])
        _write_dist(site, "mcp", _PINNED_MCP)

        violations = audit_dependency_graph("ha-mcp", search_path=[str(site)])

        assert violations == [
            DependencyViolation(
                package="mcp",
                installed=_PINNED_MCP,
                requirement="mcp>=1.24.0",
                required_by="cyc-b 1.0",
            )
        ]

    def test_unparseable_requirement_is_skipped(self, tmp_path):
        site = _site(tmp_path, "bad-requirement")
        _write_dist(
            site,
            "ha-mcp",
            "7.0.0",
            requires=["this is not a requirement!!", "mcp>=1.24.0"],
        )
        _write_dist(site, "mcp", _PINNED_MCP)

        violations = audit_dependency_graph("ha-mcp", search_path=[str(site)])

        assert [v.package for v in violations] == ["mcp"]

    def test_unparseable_installed_version_is_not_reported(self, tmp_path):
        """An unreadable version proves nothing; inventing a violation misdirects."""
        site = _site(tmp_path, "bad-version")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["mcp>=1.24.0"])
        _write_dist(site, "mcp", "not-a-version")

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == []

    def test_installed_prerelease_inside_the_range_satisfies_it(self, tmp_path):
        """An installed pre-release is a fact, not a candidate being selected."""
        site = _site(tmp_path, "prerelease")
        _write_dist(site, "ha-mcp", "7.0.0", requires=["mcp>=1.24.0"])
        _write_dist(site, "mcp", "1.25.0b1")

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == []

    def test_missing_root_distribution_is_reported_as_requested(self, tmp_path):
        site = _site(tmp_path, "no-root")

        assert audit_dependency_graph("ha-mcp", search_path=[str(site)]) == [
            DependencyViolation(
                package="ha-mcp",
                installed=None,
                requirement="ha-mcp",
                required_by="(requested)",
            )
        ]


class TestFindPinningIntegrations:
    def test_finds_the_integration_that_pins_the_package(self, tmp_path):
        _write_manifest(
            tmp_path,
            _XIAOZHI_DOMAIN,
            {
                "domain": _XIAOZHI_DOMAIN,
                "name": _XIAOZHI_NAME,
                "requirements": [f"mcp=={_PINNED_MCP}"],
            },
        )

        assert find_pinning_integrations(str(tmp_path), "mcp") == [
            PinningIntegration(
                domain=_XIAOZHI_DOMAIN,
                name=_XIAOZHI_NAME,
                requirement=f"mcp=={_PINNED_MCP}",
            )
        ]

    def test_matches_on_canonical_name_and_keeps_the_declared_string(self, tmp_path):
        _write_manifest(
            tmp_path,
            "loud_pin",
            {"name": "Loud Pin", "requirements": [f"Mcp == {_PINNED_MCP}"]},
        )
        _write_manifest(
            tmp_path,
            "extras_pin",
            {"name": "Extras Pin", "requirements": [f"mcp[cli]=={_PINNED_MCP}"]},
        )

        found = find_pinning_integrations(str(tmp_path), "mcp")

        assert [(p.domain, p.requirement) for p in found] == [
            ("extras_pin", f"mcp[cli]=={_PINNED_MCP}"),
            ("loud_pin", f"Mcp == {_PINNED_MCP}"),
        ]

    def test_name_falls_back_to_the_domain(self, tmp_path):
        _write_manifest(tmp_path, "nameless", {"requirements": ["mcp==1.14.1"]})

        assert find_pinning_integrations(str(tmp_path), "mcp")[0].name == "nameless"

    def test_excluded_domains_are_skipped(self, tmp_path):
        _write_manifest(tmp_path, "ha_mcp_tools", {"requirements": ["mcp>=1.24.0"]})
        _write_manifest(
            tmp_path, _XIAOZHI_DOMAIN, {"requirements": [f"mcp=={_PINNED_MCP}"]}
        )

        found = find_pinning_integrations(
            str(tmp_path), "mcp", exclude_domains=("ha_mcp_tools",)
        )

        assert [p.domain for p in found] == [_XIAOZHI_DOMAIN]

    def test_other_packages_are_ignored(self, tmp_path):
        _write_manifest(
            tmp_path, "unrelated", {"requirements": ["aiohttp==3.9.0", "numpy"]}
        )

        assert find_pinning_integrations(str(tmp_path), "mcp") == []

    def test_broken_manifests_do_not_hide_the_good_one(self, tmp_path):
        _write_manifest(tmp_path, "invalid_json", "{not json at all")
        _write_manifest(tmp_path, "requirements_not_a_list", {"requirements": "mcp"})
        _write_manifest(tmp_path, "bad_requirement", {"requirements": ["!!!", 17]})
        (tmp_path / "custom_components" / "no_manifest").mkdir(parents=True)
        _write_manifest(
            tmp_path, _XIAOZHI_DOMAIN, {"requirements": [f"mcp=={_PINNED_MCP}"]}
        )

        found = find_pinning_integrations(str(tmp_path), "mcp")

        assert [p.domain for p in found] == [_XIAOZHI_DOMAIN]

    def test_missing_custom_components_directory_is_tolerated(self, tmp_path):
        assert find_pinning_integrations(str(tmp_path / "nowhere"), "mcp") == []
        assert find_pinning_integrations(str(tmp_path), "mcp") == []


class TestRequirementForcesConflict:
    """Only requirements that can actually force the violation count as pinners."""

    _VIOLATION = DependencyViolation(
        package="mcp",
        installed=_PINNED_MCP,
        requirement=_FASTMCP_FLOOR,
        required_by="fastmcp-slim 3.4.6",
    )

    def test_the_incident_pin_forces_it(self):
        assert requirement_forces_conflict(f"mcp=={_PINNED_MCP}", self._VIOLATION)

    def test_a_compatible_floor_is_innocent(self):
        """The Codex-review case: ``mcp>=1`` admits compliant versions.

        Home Assistant never reinstalls a requirement the installed version
        already satisfies, so this integration cannot downgrade anything —
        blaming it points the user at the wrong uninstall.
        """
        assert not requirement_forces_conflict("mcp>=1", self._VIOLATION)

    def test_an_upper_bound_below_the_floor_forces_it(self):
        assert requirement_forces_conflict("mcp<1.20", self._VIOLATION)

    def test_a_bare_name_is_innocent(self):
        assert not requirement_forces_conflict("mcp", self._VIOLATION)

    def test_a_pin_that_never_admitted_the_installed_version_is_innocent(self):
        """A pin on a COMPLIANT version cannot have produced the bad state."""
        assert not requirement_forces_conflict("mcp==1.26.0", self._VIOLATION)

    def test_unparseable_requirement_stays_reported(self):
        assert requirement_forces_conflict("!!!", self._VIOLATION)

    def test_unprobeable_violated_spec_keeps_the_report(self):
        """An upper-bound-only violated spec yields no probe: stay conservative."""
        upper_only = DependencyViolation(
            package="mcp",
            installed=_PINNED_MCP,
            requirement="mcp<1.0",
            required_by="something 1.0",
        )
        assert requirement_forces_conflict(f"mcp=={_PINNED_MCP}", upper_only)

    def test_wildcard_floor_still_probes(self):
        wildcard = DependencyViolation(
            package="mcp",
            installed="1.9.0",
            requirement="mcp==1.24.*",
            required_by="something 1.0",
        )
        assert not requirement_forces_conflict("mcp>=1", wildcard)
        assert requirement_forces_conflict("mcp==1.9.0", wildcard)

    def test_inactive_environment_marker_is_innocent(self):
        """CodeRabbit on #2245: HA never installs a marker-inactive requirement."""
        assert not requirement_forces_conflict(
            f'mcp=={_PINNED_MCP}; python_version < "3"', self._VIOLATION
        )

    def test_active_environment_marker_still_forces(self):
        assert requirement_forces_conflict(
            f'mcp=={_PINNED_MCP}; python_version >= "3"', self._VIOLATION
        )

    def test_extra_scoped_marker_is_innocent(self):
        """``extra`` evaluates to the empty string outside an extras install.

        pip installing a manifest requirement never activates extras, so an
        ``extra == "cli"``-scoped pin is never enforced — packaging answers
        a clean False rather than raising, and innocent is the right verdict.
        """
        assert not requirement_forces_conflict(
            f'mcp=={_PINNED_MCP}; extra == "cli"', self._VIOLATION
        )

    def test_exclusive_floor_boundary_pin_forces(self):
        """CodeRabbit on #2245: the ``>`` bound itself is not a compliant probe.

        With ``mcp>1.24`` violated and 1.24 installed, a ``mcp==1.24`` pin
        preserves the conflict — probing with 1.24 would acquit it.
        """
        boundary = DependencyViolation(
            package="mcp",
            installed="1.24",
            requirement="mcp>1.24",
            required_by="something 1.0",
        )
        assert requirement_forces_conflict("mcp==1.24", boundary)
        assert not requirement_forces_conflict("mcp>=1", boundary)

    def test_floor_exclusion_still_yields_a_probe(self):
        """CodeRabbit on #2245: ``>=1.24,!=1.24`` must not empty the probes.

        The ``!=`` exclusion rejects the ``>=`` seed itself; without the
        successor candidate the empty probe list would blame the compatible
        ``mcp>=1`` integration.
        """
        excluded_floor = DependencyViolation(
            package="mcp",
            installed="1.24",
            requirement="mcp>=1.24,!=1.24",
            required_by="something 1.0",
        )
        assert not requirement_forces_conflict("mcp>=1", excluded_floor)
        assert requirement_forces_conflict("mcp==1.24", excluded_floor)

    def test_excluded_prerelease_floor_still_yields_a_probe(self):
        """CodeRabbit on #2245: a prerelease seed needs a prerelease successor.

        ``1.0rc1.0.1`` is not a valid PEP 440 version, so without stepping
        the prerelease number the excluded floor would lose every probe and
        blame the compatible integration.
        """
        excluded_rc = DependencyViolation(
            package="mcp",
            installed="1.0rc1",
            requirement="mcp>=1.0rc1,!=1.0rc1",
            required_by="something 1.0",
        )
        assert not requirement_forces_conflict("mcp>=1.0rc1", excluded_rc)
        assert requirement_forces_conflict("mcp==1.0rc1", excluded_rc)

    def test_excluded_post_and_dev_floors_still_yield_probes(self):
        """CodeRabbit on #2245: post/dev seeds need same-shape successors.

        ``1.0.post1.0.1`` and ``1.0.dev1.0.1`` are not valid PEP 440
        versions, so without stepping the terminal segment these excluded
        floors would lose every probe and blame compatible integrations.
        """
        excluded_post = DependencyViolation(
            package="mcp",
            installed="1.0.post1",
            requirement="mcp>=1.0.post1,!=1.0.post1",
            required_by="something 1.0",
        )
        assert not requirement_forces_conflict("mcp>=1", excluded_post)
        assert requirement_forces_conflict("mcp==1.0.post1", excluded_post)

        excluded_dev = DependencyViolation(
            package="mcp",
            installed="1.0.dev1",
            requirement="mcp>=1.0.dev1,!=1.0.dev1",
            required_by="something 1.0",
        )
        assert not requirement_forces_conflict("mcp>=1.0.dev1", excluded_dev)
        assert requirement_forces_conflict("mcp==1.0.dev1", excluded_dev)

    def test_direct_url_requirement_stays_attributable(self):
        """Codex on #2245: a URL reference reinstalls its artifact on every
        setup and its version cannot be inspected — never acquit it as a
        bare name, but an inactive marker still proves innocence first."""
        assert requirement_forces_conflict(
            "mcp @ https://host.invalid/mcp-1.14.1.whl", self._VIOLATION
        )
        assert not requirement_forces_conflict(
            'mcp @ https://host.invalid/mcp-1.14.1.whl ; python_version < "3"',
            self._VIOLATION,
        )

    def test_missing_package_judged_by_probes_alone(self):
        missing = DependencyViolation(
            package="mcp",
            installed=None,
            requirement=_FASTMCP_FLOOR,
            required_by="fastmcp-slim 3.4.6",
        )
        assert not requirement_forces_conflict("mcp>=1", missing)
        assert requirement_forces_conflict(f"mcp=={_PINNED_MCP}", missing)


class TestRootImportFailure:
    def test_returns_the_cause_behind_the_fastmcp_hint(self):
        """Regression for the reported incident's exception shape."""
        try:
            try:
                raise ImportError(_ICON_IMPORT_ERROR)
            except ImportError as cause:
                raise ImportError(_FASTMCP_HINT) from cause
        except ImportError as outer:
            hint = outer

        assert root_import_failure(hint) is hint.__cause__
        assert str(root_import_failure(hint)) == _ICON_IMPORT_ERROR

    def test_returns_the_deepest_import_error_not_the_first(self):
        try:
            try:
                try:
                    raise ImportError("deepest")
                except ImportError as innermost:
                    raise ImportError("middle") from innermost
            except ImportError as middle:
                raise ImportError("outermost") from middle
        except ImportError as outer:
            hint = outer

        assert str(root_import_failure(hint)) == "deepest"

    def test_follows_context_when_there_is_no_cause(self):
        try:
            try:
                raise ImportError(_ICON_IMPORT_ERROR)
            except ImportError:
                # The missing ``from`` IS the scenario: only __context__ links.
                raise RuntimeError("wrapped without from")  # noqa: B904
        except RuntimeError as outer:
            wrapper = outer

        assert wrapper.__cause__ is None
        assert root_import_failure(wrapper) is wrapper.__context__

    def test_suppressed_context_is_not_followed(self):
        try:
            try:
                raise ImportError(_ICON_IMPORT_ERROR)
            except ImportError:
                raise RuntimeError("deliberately detached") from None
        except RuntimeError as outer:
            wrapper = outer

        assert root_import_failure(wrapper) is wrapper

    def test_chain_without_an_import_error_returns_the_exception(self):
        try:
            try:
                raise ValueError("inner")
            except ValueError as cause:
                raise RuntimeError("outer") from cause
        except RuntimeError as outer:
            wrapper = outer

        assert root_import_failure(wrapper) is wrapper

    def test_cyclic_chain_terminates(self):
        first = ImportError("first")
        second = ImportError("second")
        first.__cause__ = second
        second.__cause__ = first

        assert root_import_failure(first) is second


class TestDescribeDependencyFailure:
    def test_names_every_part_of_the_incident(self):
        message = describe_dependency_failure(
            ImportError(_ICON_IMPORT_ERROR),
            [
                DependencyViolation(
                    package="mcp",
                    installed=_PINNED_MCP,
                    requirement=_FASTMCP_FLOOR,
                    required_by="fastmcp-slim 3.4.6",
                )
            ],
            [
                PinningIntegration(
                    domain=_XIAOZHI_DOMAIN,
                    name=_XIAOZHI_NAME,
                    requirement=f"mcp=={_PINNED_MCP}",
                )
            ],
        )

        for expected in (
            "Icon",
            "mcp",
            _PINNED_MCP,
            "1.24.0",
            "fastmcp-slim 3.4.6",
            _XIAOZHI_NAME,
            _XIAOZHI_DOMAIN,
            "restart",
        ):
            assert expected in message

    def test_missing_package_reads_as_missing(self):
        message = describe_dependency_failure(
            None,
            [
                DependencyViolation(
                    package="mcp",
                    installed=None,
                    requirement=_FASTMCP_FLOOR,
                    required_by="fastmcp-slim 3.4.6",
                )
            ],
            [],
        )

        assert "mcp is not installed" in message
        assert "restart" in message

    def test_missing_root_is_not_attributed_to_a_requester(self):
        message = describe_dependency_failure(
            None,
            [
                DependencyViolation(
                    package="ha-mcp",
                    installed=None,
                    requirement="ha-mcp",
                    required_by="(requested)",
                )
            ],
            [],
        )

        assert message.startswith("Package ha-mcp is not installed.")
        assert "(requested)" not in message

    def test_violation_without_a_pinner_does_not_blame_an_integration(self):
        message = describe_dependency_failure(
            None,
            [
                DependencyViolation(
                    package="mcp",
                    installed=_PINNED_MCP,
                    requirement=_FASTMCP_FLOOR,
                    required_by="fastmcp-slim 3.4.6",
                )
            ],
            [],
        )

        assert "integration" not in message
        assert "restart Home Assistant" in message

    def test_several_pinners_read_as_plural(self):
        message = describe_dependency_failure(
            None,
            [],
            [
                PinningIntegration(domain="one", name="One", requirement="mcp==1.0"),
                PinningIntegration(domain="two", name="Two", requirement="mcp==1.1"),
            ],
        )

        assert "conflicting integrations" in message
        assert "one" in message
        assert "two" in message

    def test_root_exception_alone_still_yields_an_action(self):
        message = describe_dependency_failure(ImportError(_ICON_IMPORT_ERROR), [], [])

        assert "Icon" in message
        assert "ImportError" in message
        assert "Restart Home Assistant" in message

    def test_message_less_exception_falls_back_to_its_type(self):
        message = describe_dependency_failure(ImportError(), [], [])

        assert message.startswith("The underlying failure is: ImportError.")

    def test_nothing_found_says_so(self):
        assert (
            describe_dependency_failure(None, [], [])
            == "No dependency conflict was detected."
        )
