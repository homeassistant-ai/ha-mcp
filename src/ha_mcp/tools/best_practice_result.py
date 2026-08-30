"""Warning accumulator and skill-reference routing for the best-practice checks.

Split out of :mod:`best_practice_checker` so a check module can emit a warning
without importing the checker back (.gemini/styleguide.md - Tool Consolidation and Module Size). Nothing here
inspects a config; it only carries warnings and builds the ' See ...' suffix.
"""

_SKILL_URI_PREFIX = "skill://home-assistant-best-practices/references"
_DEFAULT_SKILL_PREFIX = _SKILL_URI_PREFIX
_SKILL_NAME = "home-assistant-best-practices"


class BestPracticeCheckResult(list[str]):
    """Warning list with an attached set of referenced skill files.

    Behaves as a plain ``list[str]`` for all call sites — ``len()``,
    indexing, iteration, equality with ``[...]`` all work unchanged.
    ``referenced_files`` is added on top so the write tools can resolve
    and embed the relevant skill bodies into responses.

    Use :func:`_emit` to append warnings; direct ``append``/``extend``
    skip the ``referenced_files`` mirror. Slicing, ``list()`` coercion,
    and ``copy.copy``/``copy.deepcopy`` all return plain ``list[str]``
    without the attribute — no current call site does any of these.
    """

    __slots__ = ("referenced_files",)

    referenced_files: set[str]

    def __init__(self, items: list[str] | None = None) -> None:
        super().__init__(items or [])
        self.referenced_files = set()


# ---------------------------------------------------------------------------
# Warning emission + skill-reference helpers
# ---------------------------------------------------------------------------


def _emit(
    warnings: BestPracticeCheckResult,
    message: str,
    skill_prefix: str | None,
    file_ref: str,
) -> None:
    """Append a warning with the 2-route ' See ...' suffix and track the file.

    Args:
        warnings: Accumulator (also holds ``referenced_files``).
        message: Human-readable warning body — the inline alternative.
        skill_prefix: When set, embedded as the ``skill://`` URI route.
            When falsy, the whole ``See ...`` suffix is dropped rather than
            just that route — see :func:`_skill_route_suffix`.
        file_ref: File path relative to the ``references/`` directory of
            the home-assistant-best-practices skill, optionally with a
            ``#anchor`` suffix
            (e.g. ``"automation-patterns.md#native-conditions"``). The
            anchor is preserved end-to-end: in the ``skill://`` URI for
            display, and in ``referenced_files`` so the auto-embed path
            ships only the matching markdown section instead of the
            whole 10-20 KB reference file.
    """
    warnings.append(message + _skill_route_suffix(skill_prefix, file_ref))
    warnings.referenced_files.add(f"references/{file_ref}")


def _skill_route_suffix(skill_prefix: str | None, file_ref: str) -> str:
    """Build the ' See ...' suffix naming the available skill access routes.

    When ``skill_prefix`` is ``None`` the entire suffix is suppressed —
    skills are off server-wide, so neither route resolves. Otherwise the
    suffix names both so the LLM has a working path regardless of which
    mechanism its client supports:

    1. ``skill://`` URI — for clients that auto-fetch resource URIs.
       Anchor preserved.
    2. ``ha_get_skill_guide(skill=..., file=...)`` — explicit tool call,
       works on every MCP client. Anchor stripped (the tool reads the
       whole file).

    Auto-embed of the matching section still happens in the next
    write's response, driven by ``referenced_files``; the
    ``MandatoryBPS`` opt-out param is not named here by design.
    """
    if not skill_prefix:
        # Skills feature is disabled server-wide; none of the routes work.
        # Matches the historical no-suffix behaviour.
        return ""
    bare_file = f"references/{file_ref.split('#', 1)[0]}"
    routes = [
        f"{skill_prefix}/{file_ref}",
        f"call ha_get_skill_guide(skill={_SKILL_NAME!r}, file={bare_file!r})",
    ]
    return " See " + " | ".join(routes)
