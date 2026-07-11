"""Shared 3-tier config fetching and entry scoring for deep search."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ._config import BULK_WEBSOCKET_TIMEOUT, INDIVIDUAL_FETCH_BATCH_SIZE
from ._scoring import ScoringMixin

logger = logging.getLogger(__name__)


def is_timeout_error(exc: BaseException) -> bool:
    """True when ``exc`` is, or was directly caused by, a request timeout.

    The per-id fetchers wrap their client call in ``asyncio.wait_for``, whose
    expiry raises the builtin ``TimeoutError`` — but the REST client applies
    its own httpx timeout (``HA_TIMEOUT``, default 30s) and ``_raw_request``
    re-raises ``httpx.TimeoutException`` as ``HomeAssistantConnectionError``
    (a sibling of ``HomeAssistantAPIError``). When the httpx timeout is the
    shorter of the two — e.g. a user raised HAMCP_INDIVIDUAL_CONFIG_TIMEOUT
    past HA_TIMEOUT following the partial-result advice — the timeout
    arrives wrapped, and classifying by ``except TimeoutError`` alone would
    drop it into the generic "failed" bucket: the exact misclassification
    issue #1784 exists to eliminate. Checking ``__cause__`` (set by the
    client's ``raise ... from e``) catches the wrapped form.
    """
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return True
    return isinstance(exc.__cause__, TimeoutError | httpx.TimeoutException)


class ConfigFetchMixin(ScoringMixin):
    """REST/WebSocket bulk + budgeted individual config fetch, and scoring of fetched entries."""

    @staticmethod
    def _index_configs(
        items: list[dict[str, Any]],
        id_of: Callable[[dict[str, Any]], str | None],
    ) -> dict[str, dict[str, Any]]:
        """Build a ``{id: config}`` map, skipping items with no usable id."""
        configs: dict[str, dict[str, Any]] = {}
        for item in items:
            key = id_of(item)
            if key:
                configs[key] = item
        return configs

    async def _bulk_fetch_configs(
        self,
        rest_endpoint: str,
        ws_types: list[str],
        id_of: Callable[[dict[str, Any]], str | None],
        rest_timeout: float,
        label: str,
    ) -> dict[str, dict[str, Any]] | None:
        """Bulk-fetch all configs of one domain: REST endpoint, then WS list endpoints.

        Returns ``{id: config}`` (possibly empty) on the first successful
        attempt, or ``None`` when every attempt failed. An empty-but-successful
        REST list returns ``{}`` (not ``None``) so the caller skips the
        individual-fetch fallback exactly as it would for a populated response.
        """
        try:
            resp = await asyncio.wait_for(
                self.client._request("GET", rest_endpoint),
                timeout=rest_timeout,
            )
            if isinstance(resp, list):
                return self._index_configs(resp, id_of)
        except Exception as e:
            logger.debug(f"{label} REST bulk fetch failed: {e}")

        for ws_type in ws_types:
            try:
                ws_resp = await asyncio.wait_for(
                    self.client.send_websocket_message({"type": ws_type}),
                    timeout=BULK_WEBSOCKET_TIMEOUT,
                )
                if isinstance(ws_resp, dict) and ws_resp.get("success"):
                    return self._index_configs(ws_resp.get("result", []), id_of)
            except Exception as e:
                logger.debug(f"{label} WebSocket bulk fetch ({ws_type}) failed: {e}")
        return None

    async def _individual_fetch_budgeted(
        self,
        ids: list[str],
        fetch_one: Callable[
            [str], Awaitable[tuple[str, dict[str, Any] | None, str | None]]
        ],
        budget: float,
        label: str,
        plural: str,
    ) -> tuple[dict[str, dict[str, Any]], int, int, int, int]:
        """Fetch configs individually in parallel batches under a wall-clock budget.

        ``fetch_one(id)`` returns ``(id, config | None, fail_kind | None)``
        where ``fail_kind`` is ``None`` on success, ``"yaml_skipped"`` when
        the per-id endpoint returned 404 (the config is structurally
        unfetchable — typically a YAML-defined automation/script that the
        ``/config/<type>/config/<id>`` REST endpoint can't expose),
        ``"timeout"`` when the fetch exceeded the per-request
        ``INDIVIDUAL_CONFIG_TIMEOUT``, or ``"failed"`` for any other
        exception. New batches stop launching once ``budget`` seconds
        elapse. Returns
        ``(configs, failed_count, skipped_count, yaml_skipped_count,
        timeout_count)``.

        Counting the YAML-defined class distinctly lets callers explain to
        end users that the gap is **structural** (the config exists, the
        endpoint just can't return it) rather than a transient error. This
        mirrors the scene path's ``integration_skipped`` treatment for
        non-HA-managed scenes.

        Counting the timeout class distinctly matters for the same reason
        (issue #1784): on HA servers that serve the per-id endpoint
        serially, a batch's tail requests queue past the per-request
        timeout even though every one of them would return 200 — folding
        those into the generic "failed" bucket sends users hunting for
        broken automations that don't exist, when the fix is tuning
        ``INDIVIDUAL_FETCH_BATCH_SIZE`` / ``INDIVIDUAL_CONFIG_TIMEOUT``.

        Fetch order is NOT prioritized by name score: deep_search's purpose is
        to find matches INSIDE configs (conditions/actions), not just by name,
        so name-prioritizing would skip the configs most likely to contain
        non-obvious matches. See #879.
        """
        configs: dict[str, dict[str, Any]] = {}
        budget_start = time.perf_counter()
        total_to_fetch = len(ids)
        fetched_count = 0
        failed_count = 0
        skipped_count = 0
        yaml_skipped_count = 0
        timeout_count = 0
        for i in range(0, len(ids), INDIVIDUAL_FETCH_BATCH_SIZE):
            if time.perf_counter() - budget_start > budget:
                skipped_count = (
                    total_to_fetch
                    - fetched_count
                    - failed_count
                    - yaml_skipped_count
                    - timeout_count
                )
                logger.warning(
                    f"{label} config fetch budget exhausted ({budget}s). "
                    f"Fetched {fetched_count}/{total_to_fetch} "
                    f"({failed_count} failed, {timeout_count} timed out, "
                    f"{yaml_skipped_count} yaml-skipped), "
                    f"skipped {skipped_count} {plural}."
                )
                break
            batch = ids[i : i + INDIVIDUAL_FETCH_BATCH_SIZE]
            batch_results = await asyncio.gather(*[fetch_one(x) for x in batch])
            for key, config, fail_kind in batch_results:
                if config is not None:
                    configs[key] = config
                    fetched_count += 1
                elif fail_kind == "yaml_skipped":
                    yaml_skipped_count += 1
                elif fail_kind == "timeout":
                    timeout_count += 1
                else:
                    failed_count += 1
        return configs, failed_count, skipped_count, yaml_skipped_count, timeout_count

    def _score_config_entries(
        self,
        scored: list[tuple[str, str, str | None, int]],
        configs: dict[str, dict[str, Any]],
        query_lower: str,
        exact_match: bool,
    ) -> list[dict[str, Any]]:
        """Score each ``(entity_id, friendly_name, key, name_score)`` against its config.

        Returns one raw match record per entry clearing its threshold. Each
        per-type caller maps these records into its own result shape.
        """
        matches: list[dict[str, Any]] = []
        for entity_id, friendly_name, key, name_score in scored:
            config = configs.get(key, {}) if key else {}
            config_match_score = (
                self._search_in_dict(config, query_lower, exact_match) if config else 0
            )
            total_score, threshold, match_in_name = self._score_deep_match(
                entity_id,
                friendly_name,
                name_score,
                config_match_score,
                query_lower,
                exact_match,
            )
            if total_score >= threshold:
                matches.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "key": key,
                        "config": config,
                        "score": total_score,
                        "match_in_name": match_in_name,
                        "match_in_config": config_match_score >= threshold,
                    }
                )
        return matches
