"""Name the dependency conflict behind an embedded-server import failure (issue #2239).

Home Assistant enforces every custom integration's ``manifest.json``
``requirements`` at every startup, in one shared site-packages tree. A third
party integration that pins a package this component's server also depends on
therefore *downgrades that package for us*, silently, on a schedule we do not
control — and the resulting failure surfaces far from its cause. The reported
incident: an integration pinning ``mcp==1.14.1`` dropped the shared ``mcp``
below fastmcp's ``>=1.24.0`` floor, fastmcp's resource module failed on
``from mcp.types import Icon``, and fastmcp re-raised that as the generic hint
"FastMCP server support is not installed" — which names neither the package,
nor the version, nor the integration that moved it.

This module turns that class of failure into a sentence a user can act on. It
is deliberately three independent, side-effect-free steps, so each can be
tested (and reused) on its own:

* :func:`root_import_failure` digs the real error out of an exception chain
  whose outermost message is a misleading fallback,
* :func:`audit_dependency_graph` walks the installed requirement graph from a
  root distribution and reports every requirement the installed tree does not
  satisfy,
* :func:`find_pinning_integrations` scans ``custom_components/*/manifest.json``
  for the integration whose pin caused it, and
* :func:`describe_dependency_failure` composes the three into one message.

Nothing here imports Home Assistant, or any other module of this component: the
diagnosis has to run in exactly the situation where the dependency tree is
already broken, so it may depend only on the standard library and ``packaging``
(a Home Assistant runtime dependency, always importable). Every function is
total — malformed metadata, unreadable files and unparseable version strings
produce a smaller answer, never an exception, because a diagnostic that raises
inside an error handler replaces the failure it was meant to explain.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path

from packaging.markers import Marker, UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# ``required_by`` for the root distribution itself: nothing in the installed
# graph declared it, the caller asked for it. Rendered without an attribution
# clause, which would read as a package named "(requested)".
REQUESTED_BY = "(requested)"

# Synthesized compliance-probe successors chained per seed. Seeds include
# every clause's version — a ``!=`` exclusion names the exact point to step
# past, so any finite set of pointwise exclusions is defeated by
# construction (each excluded point seeds its own successor); the chain
# depth only helps mixed shapes. Past that, a RANGE is doing the excluding,
# where an empty probe list is the honest answer.
_SUCCESSOR_STEPS = 3


@dataclass(frozen=True, slots=True)
class DependencyViolation:
    """One requirement the installed distribution tree does not satisfy."""

    package: str
    """Canonical name of the unsatisfied distribution, e.g. ``"mcp"``."""

    installed: str | None
    """Installed version, or None when the distribution is missing entirely."""

    requirement: str
    """The violated requirement, normalized, e.g. ``"mcp<2.0,>=1.24.0"``."""

    required_by: str
    """``"<dist> <version>"`` that declared it, e.g. ``"fastmcp-slim 3.4.6"``."""


@dataclass(frozen=True, slots=True)
class PinningIntegration:
    """A custom integration whose manifest pins a package we also depend on."""

    domain: str
    """The ``custom_components`` subdirectory name."""

    name: str
    """The manifest's ``name``, falling back to ``domain``."""

    requirement: str
    """The matching requirement as declared, e.g. ``"mcp==1.14.1"``."""


def audit_dependency_graph(
    root_dist: str, *, search_path: list[str] | None = None
) -> list[DependencyViolation]:
    """Report every unsatisfied requirement reachable from ``root_dist``.

    Breadth-first over the *installed* requirement graph: each distribution's
    ``Requires-Dist`` entries are checked against what is actually installed,
    which is the question a resolver never gets asked again once Home Assistant
    has reinstalled a pin behind its back.

    ``search_path`` is the metadata search path (``None`` = the default
    ``sys.path`` behavior); it exists so tests can point at a synthetic tree.
    The distribution map is built once, so the walk costs one metadata scan
    however wide the graph is.

    Extras are tracked per edge: a requirement scoped ``extra == "server"``
    applies only on a path that pulled its distribution in as ``dist[server]``,
    so the visited set is keyed on ``(name, active extras)`` rather than name
    alone. That both terminates cycles and keeps a diamond from being judged
    under the wrong extra set. Requirements are still *checked* under every
    extra set that reaches them, so equal violations found twice are collapsed.

    A missing distribution is reported and not recursed into (it declares
    nothing we can read). An installed-but-wrong version is reported *and*
    recursed into, since its own requirements are equally suspect.
    Unparseable requirement strings are skipped and unparseable installed
    versions are treated as satisfying: neither proves a violation, and a
    diagnostic that invents one sends the user after the wrong package.
    """
    installed = _installed_distributions(search_path)
    root = canonicalize_name(root_dist)
    if root not in installed:
        return [
            DependencyViolation(
                package=root,
                installed=None,
                requirement=root_dist,
                required_by=REQUESTED_BY,
            )
        ]

    found: dict[DependencyViolation, None] = {}
    visited: set[tuple[str, frozenset[str]]] = set()
    queue: deque[tuple[str, frozenset[str]]] = deque([(root, frozenset())])
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        name, extras = node
        dist = installed.get(name)
        if dist is None:
            continue
        declared_by = _dist_label(name, dist)
        for requirement in _applicable_requirements(dist, extras):
            child = canonicalize_name(requirement.name)
            child_dist = installed.get(child)
            if child_dist is None:
                found[
                    DependencyViolation(
                        package=child,
                        installed=None,
                        requirement=str(requirement),
                        required_by=declared_by,
                    )
                ] = None
                continue
            version = _dist_version(child_dist)
            # A direct reference has no specifier to judge, but an installed
            # dist with NO PEP 610 origin record was definitively installed
            # from an index, not the referenced artifact — a sound origin
            # mismatch with no fragile URL comparison (Codex on #2245; a
            # present-but-different origin stays unjudged, since pip
            # normalizes URLs and a re-hosted identical artifact is fine).
            if requirement.url is not None:
                # version non-None on this branch too: a dist whose METADATA
                # yields no version would otherwise render as "not installed"
                # in the violation sentence (Patch76 review on #2245).
                unsatisfied = version is not None and not _dist_has_direct_url(
                    child_dist
                )
            else:
                unsatisfied = version is not None and not _specifier_allows(
                    requirement, version
                )
            if unsatisfied:
                found[
                    DependencyViolation(
                        package=child,
                        installed=version,
                        requirement=str(requirement),
                        required_by=declared_by,
                    )
                ] = None
            queue.append(
                (child, frozenset(canonicalize_name(e) for e in requirement.extras))
            )

    return sorted(found, key=lambda v: (v.package, v.required_by, v.requirement))


def find_pinning_integrations(
    config_dir: str, package: str, *, exclude_domains: tuple[str, ...] = ()
) -> list[PinningIntegration]:
    """Find custom integrations whose manifest requires ``package``.

    Matching is on the canonicalized project name, so ``Mcp == 1.14.1`` and
    ``mcp[cli]==1.14.1`` are found for ``package="mcp"``. Every requirement on
    the package is reported, not only exact pins: a range that excludes the
    version we need conflicts just as effectively. This is the raw scan —
    callers attributing a concrete violation drop the innocent matches with
    :func:`requirement_forces_conflict`.

    ``exclude_domains`` drops known-innocent domains — this component's own,
    above all, which legitimately declares the same dependency.

    A missing ``custom_components`` directory, an unreadable manifest, invalid
    JSON, a non-list ``requirements`` and an unparseable requirement each yield
    nothing rather than raising: this runs while an install is already broken,
    and half an answer beats a second traceback. Results are ordered by domain,
    then by manifest order.
    """
    target = canonicalize_name(package)
    excluded = set(exclude_domains)
    try:
        domains = sorted(
            entry
            for entry in (Path(config_dir) / "custom_components").iterdir()
            if entry.is_dir()
        )
    except OSError:
        return []

    pinners: list[PinningIntegration] = []
    for domain in domains:
        if domain.name in excluded:
            continue
        manifest = _read_manifest(domain / "manifest.json")
        if manifest is None:
            continue
        declared = manifest.get("requirements")
        if not isinstance(declared, list):
            continue
        name = manifest.get("name")
        for raw in declared:
            if _requirement_project_name(raw) != target:
                continue
            pinners.append(
                PinningIntegration(
                    domain=domain.name,
                    name=name if isinstance(name, str) and name else domain.name,
                    requirement=raw,
                )
            )
    return pinners


def requirement_forces_conflict(
    requirement: str, violation: DependencyViolation
) -> bool:
    """Whether enforcing ``requirement`` can produce or preserve ``violation``.

    Home Assistant reinstalls a manifest requirement only when the installed
    version does not satisfy it, so an integration whose requirement admits
    the versions the violated specifier needs never forces the conflict —
    blaming it sends the user to uninstall the wrong integration. Two
    innocence proofs, either sufficient:

    * the requirement did not admit the violating installed version, so it
      cannot have produced this state, or
    * the requirement also admits every compliance probe (the version
      literals a compliant install could sit at, read from the violated
      specifier's own lower-bound clauses), so enforcing it never moves a
      compliant install.

    Unjudgeable inputs — an unparseable requirement, a violated specifier
    with no probeable clause — count as forcing: a culprit hidden by a parse
    gap is worse than one report too many, which the user can dismiss.
    A bare name with no specifier is always innocent; it is satisfied by
    whatever is installed. So is a requirement whose environment marker is
    inactive on this interpreter: Home Assistant evaluates the marker and
    never installs the requirement at all. A marker that cannot be evaluated
    keeps the conservative path — the specifier is still judged.
    """
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return True
    if _marker_inactive(parsed.marker):
        return False
    if parsed.url is not None:
        # A direct reference reinstalls its artifact on every setup (HA's
        # is-installed check answers False for any URL requirement), and the
        # artifact's version cannot be inspected here — conservatively
        # attributable, never acquitted as a bare name.
        return True
    if not list(parsed.specifier):
        return False
    if violation.installed is not None:
        try:
            Version(violation.installed)
        except InvalidVersion:
            pass
        else:
            if not parsed.specifier.contains(violation.installed, prereleases=True):
                return False
    probes = _compliance_probes(violation.requirement)
    if not probes:
        return True
    return not all(
        parsed.specifier.contains(probe, prereleases=True) for probe in probes
    )


def _marker_inactive(marker: Marker | None) -> bool:
    """Whether ``marker`` provably does NOT apply on this interpreter.

    Only a clean False evaluation proves inactivity; no marker, a True one,
    and one that cannot be evaluated all return False so the caller keeps
    judging the specifier — the conservative direction.
    """
    if marker is None:
        return False
    try:
        return not marker.evaluate()
    except (UndefinedComparison, UndefinedEnvironmentName):
        return False


def _compliance_probes(violated: str) -> list[str]:
    """Version literals a compliant install could sit at, read from ``violated``.

    EVERY clause seeds candidates — its version plus a bounded successor
    chain. Lower bounds name the floor; a ``!=`` exclusion names the exact
    point to step past, which is what terminates the pointwise-exclusion
    family for good: each excluded point seeds its own successor, so a
    finite exclusion set can never empty the probes (CodeRabbit on #2245,
    three rounds of it: ``>1.24`` probed with ``1.24`` acquitted a
    ``==1.24`` pinner; ``>=1.24,!=1.24`` yielded no probe; a four-exclusion
    spec exhausted a fixed chain). Upper bounds contribute candidates the
    filter drops. A wildcard pin's trailing ``.*`` is stripped so the
    candidate parses as a version, and every candidate is checked against
    the FULL violated specifier before it may serve as a probe: a probe
    that violates the specifier would acquit exactly the pin that preserves
    the conflict, and an empty probe list flips the caller conservative,
    blaming integrations that are compatible. A compliant region reachable
    only strictly between adjacent named points remains conservatively
    unprobed — the stated boundary of this scheme.

    NAMED versions outrank synthesized ones: when any seed the violated
    specifier itself names survives the filter, only those serve as probes,
    and the nearby higher successors stand in solely when none do (an
    exclusive ``>`` bound, a floor exclusion). A verdict must never be
    decided by a version the probe invented while a named one exists —
    ``mcp<=1.24.0`` admits the named floor of ``mcp<2.0,>=1.24.0`` and
    cannot hold the package below it, but rejecting the synthesized
    ``1.24.0.0.1`` used to get it blamed (Patch76 review on #2245).
    """
    try:
        specifier = Requirement(violated).specifier
    except InvalidRequirement:
        return []
    named: list[str] = []
    synthesized: list[str] = []
    for clause in specifier:
        base = clause.version.rstrip(".*").rstrip(".")
        try:
            parsed = Version(base)
        except InvalidVersion:
            continue
        named.append(base)
        step = parsed
        for _ in range(_SUCCESSOR_STEPS):
            candidate = _successor(step)
            synthesized.append(candidate)
            try:
                step = Version(candidate)
            except InvalidVersion:
                break

    def _surviving(candidates: list[str]) -> list[str]:
        # contains() answers False for a candidate it cannot parse (the
        # same quirk _specifier_allows documents), so a successor shape
        # that fails to parse is filtered here, never raised.
        probes: list[str] = []
        for candidate in candidates:
            if candidate not in probes and specifier.contains(
                candidate, prereleases=True
            ):
                probes.append(candidate)
        return probes

    return _surviving(named) or _surviving(synthesized)


def _successor(version: Version) -> str:
    """A nearby strictly-higher version sharing ``version``'s PEP 440 shape.

    The TERMINAL segment steps — dev, then post, then prerelease number —
    because release segments cannot follow any of those tags, so the release
    form's ``.0.1`` suffix would not parse and an excluded dev/post/
    prerelease floor would lose its probe (CodeRabbit on #2245, both the
    prerelease and the post/dev shapes). A plain release gains a trailing
    ``.0.1`` segment — the smallest step an upper bound tighter than one
    release segment still admits.
    """
    epoch = f"{version.epoch}!" if version.epoch else ""
    release = ".".join(str(part) for part in version.release)
    pre = f"{version.pre[0]}{version.pre[1]}" if version.pre is not None else ""
    if version.dev is not None:
        post = f".post{version.post}" if version.post is not None else ""
        return f"{epoch}{release}{pre}{post}.dev{version.dev + 1}"
    if version.post is not None:
        return f"{epoch}{release}{pre}.post{version.post + 1}"
    if version.pre is not None:
        return f"{epoch}{release}{version.pre[0]}{version.pre[1] + 1}"
    return f"{version}.0.1"


def root_import_failure(exc: BaseException) -> BaseException:
    """Return the deepest :class:`ImportError` in ``exc``'s chain.

    The incident this exists for: fastmcp catches the real
    ``ImportError: cannot import name 'Icon' from 'mcp.types'`` and re-raises
    a generic "FastMCP server support is not installed" hint with the real
    error as ``__cause__``. Only the hint reaches the user, and it points at
    the one thing that is not wrong.

    ``__cause__`` wins over ``__context__`` (an explicit ``raise ... from`` is
    a stronger claim than an incidental nesting), and ``__context__`` is
    followed only when it was not suppressed by ``raise ... from None``.
    ``exc`` is returned unchanged when the chain holds no ``ImportError`` at
    all, so callers can pass any exception through this. A cycle in the chain
    terminates rather than hangs.
    """
    deepest: BaseException | None = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImportError):
            deepest = current
        following = current.__cause__
        if following is None and not current.__suppress_context__:
            following = current.__context__
        current = following
    return deepest if deepest is not None else exc


def describe_dependency_failure(
    root_exc: BaseException | None,
    violations: list[DependencyViolation],
    pinners: list[PinningIntegration],
) -> str:
    """Compose one user-actionable message from the three diagnosis steps.

    Ordered cause-first: the real error, then the requirement it violates,
    then who moved the package, then what to do. Every combination of empty
    and non-empty inputs produces a sensible message — the parts are
    independent, and the closing action adapts to how much was identified,
    since telling a user to uninstall an integration nobody found is worse
    than saying nothing.
    """
    sentences: list[str] = []
    if root_exc is not None:
        sentences.append(f"The underlying failure is: {_exception_text(root_exc)}.")
    sentences += [_violation_sentence(violation) for violation in violations]
    sentences += [_pinner_sentence(pinner) for pinner in pinners]
    if not sentences:
        return "No dependency conflict was detected."
    sentences.append(_action_sentence(violations, pinners))
    return " ".join(sentences)


def _installed_distributions(
    search_path: list[str] | None,
) -> dict[str, Distribution]:
    """Map canonical distribution name to Distribution, first match winning.

    First-wins mirrors import resolution: with the same name installed twice
    on the path, the earlier entry is the one that gets imported, so it is the
    one whose version the failure is about. Distributions whose metadata does
    not even yield a name are skipped -- there is nothing to match them by.
    """
    # distributions(path=None) is not the default: Context.path returns the
    # None it was handed rather than sys.path, and the finder then fails on it.
    scan = distributions() if search_path is None else distributions(path=search_path)
    found: dict[str, Distribution] = {}
    for dist in scan:
        try:
            raw_name = dist.name
        except Exception:  # corrupt METADATA must not abort the whole scan
            continue
        if raw_name:
            found.setdefault(canonicalize_name(raw_name), dist)
    return found


def _applicable_requirements(
    dist: Distribution, extras: frozenset[str]
) -> Iterator[Requirement]:
    """Yield ``dist``'s requirements that apply under ``extras``."""
    try:
        declared = dist.requires
    except Exception:  # corrupt METADATA yields no edges, not a crash
        return
    for raw in declared or []:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            continue
        if _marker_applies(requirement.marker, extras):
            yield requirement


def _marker_applies(marker: Marker | None, extras: frozenset[str]) -> bool:
    """Whether ``marker`` holds for this interpreter under ``extras``.

    Each active extra is evaluated in turn (a requirement applies if *any* of
    them activates it); with no extras active, ``extra`` is bound to the empty
    string, which is what an ``extra == "server"`` marker needs to evaluate to
    False instead of raising ``UndefinedEnvironmentName``.

    A marker that cannot be evaluated -- an unknown variable, an incomparable
    operand -- is treated as applying. The cost of checking a requirement that
    did not apply is a violation the user can dismiss; the cost of skipping one
    that did is the failure going unexplained.
    """
    if marker is None:
        return True
    for extra in sorted(extras) or [""]:
        try:
            if marker.evaluate({"extra": extra}):
                return True
        except (UndefinedComparison, UndefinedEnvironmentName):
            return True
    return False


def _specifier_allows(requirement: Requirement, version: str) -> bool:
    """Whether ``version`` satisfies ``requirement``'s specifier.

    ``prereleases=True`` because the installed version is a fact, not a
    candidate being selected: a pre-release that is already installed and
    inside the range satisfies it. An unparseable installed version returns
    True -- it proves nothing, and a false violation misdirects the user.
    Validated explicitly because ``SpecifierSet.contains()`` answers False
    for an unparseable version rather than raising (the same trap
    ``_pin_moves_off_installed`` in ``embedded_server.py`` documents).
    """
    try:
        Version(version)
    except InvalidVersion:
        return True
    return requirement.specifier.contains(version, prereleases=True)


def _dist_has_direct_url(dist: Distribution) -> bool:
    """Whether ``dist`` records a PEP 610 direct-URL install origin.

    Unreadable metadata answers True: absence is the only signal strong
    enough to prove an origin mismatch, and "could not read" must not be
    mistaken for "provably from an index".
    """
    try:
        return dist.read_text("direct_url.json") is not None
    except Exception:
        return True


def _dist_version(dist: Distribution) -> str | None:
    """``dist``'s version, or None when its metadata does not provide one."""
    try:
        version = dist.version
    except Exception:  # corrupt METADATA reads as "version unknown"
        return None
    return version if isinstance(version, str) and version else None


def _dist_label(name: str, dist: Distribution) -> str:
    """``"<name> <version>"`` attribution, dropping an unknown version."""
    version = _dist_version(dist)
    return f"{name} {version}" if version else name


def _read_manifest(path: Path) -> dict[str, object] | None:
    """Parse a ``manifest.json``, or None when it cannot be read as an object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        manifest = json.loads(raw)
    except ValueError:
        return None
    return manifest if isinstance(manifest, dict) else None


def _requirement_project_name(raw: object) -> str | None:
    """Canonical project name a manifest requirement entry names, or None."""
    if not isinstance(raw, str):
        return None
    try:
        return canonicalize_name(Requirement(raw).name)
    except InvalidRequirement:
        return None


def _exception_text(exc: BaseException) -> str:
    """``"<Type>: <message>"``, or the type alone for a message-less exception."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _violation_sentence(violation: DependencyViolation) -> str:
    """One sentence naming an unsatisfied requirement and who declared it."""
    requirement = _redact_requirement(violation.requirement)
    if violation.installed is None:
        if violation.required_by == REQUESTED_BY:
            return f"Package {violation.package} is not installed."
        return (
            f"Package {violation.package} is not installed, but "
            f"'{requirement}' is required by {violation.required_by}."
        )
    return (
        f"Installed {violation.package} {violation.installed} does not satisfy "
        f"'{requirement}' required by {violation.required_by}."
    )


def _redact_requirement(raw: str) -> str:
    """``raw`` with URL credentials and query parameters removed.

    Requirement strings reach user-facing warnings and repair issues —
    text users paste into public bug reports — and a direct reference may
    embed tokens (``mcp @ https://user:token@host/pkg.whl?sig=...``).
    Textual redaction rather than URL parsing keeps this total: an input
    no parser accepts still comes back with its userinfo and query gone.
    """
    if "://" not in raw:
        return raw
    redacted = re.sub(r"://[^/@\s]*@", "://", raw)
    return re.sub(r"\?\S*", "", redacted)


def _pinner_sentence(pinner: PinningIntegration) -> str:
    """One sentence naming a pinning integration and why the pin keeps coming back.

    The enforcement claim is conditional on purpose: the manifest scan is
    directory-wide, a dormant integration's requirement is only processed
    once something sets it up, and even then Home Assistant reinstalls it
    only when the installed version does not satisfy it.
    """
    return (
        f"The custom integration '{pinner.name}' ({pinner.domain}) pins "
        f"'{_redact_requirement(pinner.requirement)}' in its manifest, and "
        f"Home Assistant may reinstall that requirement when the "
        f"integration's setup finds it unsatisfied."
    )


def _action_sentence(
    violations: list[DependencyViolation], pinners: list[PinningIntegration]
) -> str:
    """The closing instruction, scaled to how much the diagnosis identified."""
    if pinners:
        # Distinct domains, not entries: one integration pinning two
        # violated packages is still one integration to update or remove
        # (Patch76 review on #2245).
        domains = {pinner.domain for pinner in pinners}
        subject = "integration" if len(domains) == 1 else "integrations"
        # The reinstall clause is load-bearing on the no-install fast path
        # (a pinned server spec, or auto-update off): removing the
        # integration deletes its pin but restores nothing, and the next
        # bring-up re-resolves no dependencies (Codex on #2245).
        return (
            f"Update or uninstall the conflicting {subject}, then restart "
            "Home Assistant. If the same failure returns after the restart, "
            "also reinstall or update the HA-MCP server package so its "
            "dependencies are resolved again — removing the integration "
            "does not restore the downgraded package by itself."
        )
    if violations:
        return (
            "Install a version of each package named above that satisfies its "
            "requirement, then restart Home Assistant."
        )
    return "Restart Home Assistant and check the log for the failing import."
