"""Deep search over UI-created flow-based helpers (template, group, …)."""

import asyncio
import logging
import re
from typing import Any

from ..helper_flows import core_helper_domains, helper_flow_types
from ..ignored_entries import normalize_ignored_entry
from ..tools_integrations import fetch_entry_options_with_status
from ._scoring import ScoringMixin

logger = logging.getLogger(__name__)


class FlowHelperSearchMixin(ScoringMixin):
    """Flow-helper config entries: listed over REST, bodies read from Home
    Assistant's own helper flows only (#2987)."""

    async def _search_flow_helpers(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        *,
        include_config: bool,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Search UI-created flow-based helpers (template, group, …).

        Flow-helpers live as config entries (not storage records) and have
        no ``<type>/list`` endpoint. Lists them via the standard config
        entries REST endpoint, then probes each entry's options flow so the
        helper's current config — template body, group members, source
        entity, etc. — is searchable. An entry without an options flow (otp)
        is matched on its title and domain. Only Home Assistant's own helper
        flows are probed: a custom helper integration's options flow is its
        own code and may act when opened (#2987), so its entries are matched
        on title and domain and counted as unsearched; when Core's catalog of
        its own helpers cannot be read, no flow is opened.

        Cost: the entries call, the cached helper-flow and own-helper lists,
        and one options-flow probe per probed entry, parallelised under
        ``semaphore``. The probe is skipped when the title alone already
        scores the maximum (a deeper config match can only raise the total,
        never lower it).

        Returns ``(results, failed_count, unsearched)``. ``failed_count``
        counts flow-helper backend failures so the caller can route them to
        ``partial``: the whole surface unreachable (the config-entries or
        helper-flow read raised, or the entries came back in an unexpected
        shape) counts as 1; otherwise it is the number of per-entry probes that
        failed (the flow raised or returned a non-form first step).
        ``unsearched`` counts the entries whose body a probe would have read
        but whose flow is not opened. Per-entry *scoring* failures (a bug
        processing a response the backend did return) are logged at warning
        and dropped without counting — one bad entry must not sink the gather.
        """
        flow_entries = await self._flow_helper_entries()
        if flow_entries is None:
            return [], 1, 0
        if not flow_entries:
            return [], 0, 0
        try:
            own_domains: frozenset[str] = await core_helper_domains(self.client)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"flow-helper search: own-helper read failed: {exc}")
            own_domains = frozenset()

        scored = await asyncio.gather(
            *(
                self._score_flow_entry(
                    e,
                    query_lower,
                    exact_match,
                    semaphore,
                    include_config,
                    may_probe=e.get("domain") in own_domains,
                )
                for e in flow_entries
            ),
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        probe_failures = unsearched = 0
        for item in scored:
            if isinstance(item, BaseException) and not isinstance(item, Exception):
                # Never swallow cancellation/shutdown into a probe-failure log.
                raise item
            if isinstance(item, tuple):
                result, probe_failed, body_unsearched = item
                if result is not None:
                    out.append(result)
                probe_failures += probe_failed
                unsearched += body_unsearched
            elif isinstance(item, Exception):
                # A scoring/extraction bug (e.g. a shape assumption breaking on
                # a future HA version) — the backend DID respond, our code
                # failed to score it. Log at warning so it's discoverable; do
                # not count it toward partial (it is a code bug, not a backend
                # outage, and the partial_reason wording is backend-specific).
                # One bad entry must not sink the whole multi-source deep_search.
                logger.warning(f"flow-helper scoring failed: {item!r}")
        return out, probe_failures, unsearched

    async def _flow_helper_entries(self) -> list[dict[str, Any]] | None:
        """The config entries of the helper flows Home Assistant lists; None
        when the entries or the flow list could not be read."""
        try:
            response = await self.client._request("GET", "/config/config_entries/entry")
            flows = await helper_flow_types(self.client)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"flow-helper search: entries or flows read failed: {exc}")
            return None
        if not isinstance(response, list):
            logger.debug(
                "flow-helper search: list_entries returned unexpected shape "
                f"({type(response).__name__}); treating as backend failure"
            )
            return None
        return [e for e in response if isinstance(e, dict) and e.get("domain") in flows]

    async def _score_flow_entry(
        self,
        entry: dict[str, Any],
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        include_config: bool,
        *,
        may_probe: bool = True,
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        """Score one flow-helper config entry, probing its options flow as needed.

        Returns ``(match | None, probe_failed, unsearched)``. ``probe_failed``
        is True when the probe could not read this entry's config body (the
        flow raised or returned a non-form first step) — distinct from a
        genuinely-empty options form. ``unsearched`` is True when a probe was
        needed but ``may_probe`` is False: the body was never searched, so a
        non-match is a *false* "no match" the caller must surface as
        ``partial`` rather than a clean zero; with ``include_config`` the
        result then carries ``config: None`` and ``options_withheld``, as the
        component's records do. A malformed entry (no string ``entry_id``) is
        skipped before the probe and is neither.
        """
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str):
            return None, False, False
        domain = entry.get("domain", "")
        title = entry.get("title") or entry_id

        # Score the name against a title-derived slug, never the opaque
        # config-entry ULID: a random ULID substring would otherwise produce
        # false-positive name matches (e.g. a 3-char query that happens to occur
        # inside the base32 id). The slug mirrors the storage-helper path, which
        # scores a name-derived id rather than an opaque key. entry_id is still
        # returned to the caller; it just isn't a search target.
        title_slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        title_pseudo_eid = f"{domain}.{title_slug}" if title_slug else domain
        name_score = self.fuzzy_searcher._calculate_entity_score(
            title_pseudo_eid, title, domain, query_lower
        )

        options: dict[str, Any] = {}
        probe_failed = False
        # Only an entry with an options flow (an ignored one has none) has a
        # body to probe. The probe can only raise the score, so a perfect title
        # match (100) skips it unless include_config asks for the body.
        normalize_ignored_entry(entry)
        need_probe = bool(entry.get("supports_options")) and (
            include_config
            or self._score_deep_match(
                title_pseudo_eid, title, name_score, 0, query_lower, exact_match
            )[0]
            < 100
        )
        unsearched = need_probe and not may_probe
        if need_probe and may_probe:
            async with semaphore:
                options, probe_ok = await fetch_entry_options_with_status(
                    self.client, entry_id, quiet=True
                )
                probe_failed = not probe_ok

        # Search the title, domain, and probed options — but not the opaque
        # entry_id (it would match random ULID substrings; it is returned in the
        # result for the caller regardless).
        haystack: dict[str, Any] = {
            "title": title,
            "domain": domain,
            "options": options,
        }
        config_score = self._search_in_dict(haystack, query_lower, exact_match)
        total_score, threshold, match_in_name = self._score_deep_match(
            title_pseudo_eid, title, name_score, config_score, query_lower, exact_match
        )
        if total_score < threshold:
            return None, probe_failed, unsearched

        result: dict[str, Any] = {
            "entry_id": entry_id,
            "helper_type": domain,
            "name": title,
            "score": total_score,
            "match_in_name": match_in_name,
            "match_in_config": config_score >= threshold,
            # See the collection-helper builder: always False for helpers,
            # emitted so the key is present on every config record.
            "match_in_references": False,
        }
        if include_config:
            result["config"] = None if unsearched else options
            if unsearched:
                result["options_withheld"] = "custom_integration"
        return result, probe_failed, unsearched
