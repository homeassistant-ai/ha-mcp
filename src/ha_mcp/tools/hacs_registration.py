"""HACS repository scoring and registration-wait machinery.

Extracted from ``tools_hacs.py`` (which holds the tool surface and its
action handlers) when that module crossed the ~1000-line split threshold.
``tools_hacs`` re-imports the public names, so established patch targets
(``ha_mcp.tools.tools_hacs.wait_for_repo_registration``) keep working.
"""

import asyncio
import logging
import time
from typing import Any

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)

# HACS uses different category names internally vs what users expect
# User-friendly name -> HACS internal name
CATEGORY_MAP = {
    "lovelace": "plugin",  # HACS calls Lovelace cards "plugin"
    "integration": "integration",
    "theme": "theme",
    "appdaemon": "appdaemon",
    "python_script": "python_script",
    "template": "template",
}

# Reverse mapping for display
CATEGORY_DISPLAY = {v: k for k, v in CATEGORY_MAP.items()}
CATEGORY_DISPLAY["plugin"] = "lovelace"  # Display as lovelace for users


def _score_repo_against_query(
    query_lower: str, name: str, full_name: str, description: str, authors: str
) -> int:
    """Compute a relevance score for a repo's text fields against a query."""
    score = 0
    if query_lower in name:
        score += 100
    if query_lower in full_name:
        score += 50
    if query_lower in description:
        score += 30
    if query_lower in authors:
        score += 20
    return score


def _filter_and_score_repos(
    all_repositories: list[dict[str, Any]],
    query: str,
    installed_only: bool | None,
) -> list[dict[str, Any]]:
    """Filter repositories and compute relevance scores."""
    query_lower = query.lower().strip()
    matches = []

    for repo in all_repositories:
        if installed_only and not repo.get("installed", False):
            continue

        # Handle None values safely
        name = (repo.get("name") or "").lower()
        description = (repo.get("description") or "").lower()
        full_name = (repo.get("full_name") or "").lower()
        authors_list = repo.get("authors") or []
        authors = " ".join(authors_list).lower()

        # Calculate relevance score (all repos match when query is empty)
        if query_lower:
            score = _score_repo_against_query(
                query_lower, name, full_name, description, authors
            )
            if score == 0:
                continue
        else:
            score = 0

        # Map HACS internal category back to user-friendly name
        repo_category = repo.get("category", "")
        display_category = CATEGORY_DISPLAY.get(repo_category, repo_category)
        entry: dict[str, Any] = {
            "name": repo.get("name"),
            "full_name": repo.get("full_name"),
            "description": repo.get("description"),
            "category": display_category,
            "id": repo.get("id"),
            "stars": repo.get("stars", 0),
            "downloads": repo.get("downloads", 0),
            "authors": authors_list,
            "installed": repo.get("installed", False),
            "installed_version": repo.get("installed_version")
            if repo.get("installed")
            else None,
            "available_version": repo.get("available_version"),
        }
        if query_lower:
            entry["score"] = score
        if repo.get("installed"):
            entry["pending_update"] = repo.get("pending_upgrade", False)
            entry["domain"] = repo.get("domain")
        matches.append(entry)

    # Sort by score (descending) when searching, by name when listing
    if query_lower:
        matches.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        matches.sort(key=lambda x: (x.get("name") or "").lower())

    return matches


# HACS' dispatcher signal that fires whenever a repository is
# registered, installed, or otherwise mutates. Raw string keeps
# ha-mcp's runtime free of a hard import on HACS internals — the
# load-bearing contract is this signal name, not where it's defined.
HACS_REPOSITORY_SIGNAL = "hacs_dispatch_repository"

# Budget for the initial ``hacs/subscribe`` ack, separate from the
# caller-supplied registration budget so a slow subscribe surfaces as
# its own failure instead of silently consuming the wait — subscribe
# acks return in milliseconds in practice, so 10 s is generous.
HACS_SUBSCRIBE_TIMEOUT = 10.0

# Backstop poll cadence inside ``wait_for_repo_registration`` —
# between dispatcher nudges we re-check ``hacs/repositories/list``
# at this cadence so we still complete if HACS' dispatch is dropped/
# lossy for any reason. Larger than the old 1.0 s because we expect
# the nudge to do the heavy lifting; this is belt-and-braces only.
HACS_REPO_BACKSTOP_POLL_INTERVAL = 5.0

# Wall-clock budget for confirming a custom repository registered after an
# ``hacs/repositories/add``. Shorter than the resolve/download budget: a valid
# repo registers in seconds, and failing fast turns an accepted-but-never-
# registered add (archived/invalid repo, wrong category) into a prompt error
# instead of a 30 s stall. Not exercised by the e2e suite (the only e2e add
# fails at the owner/repo format guard), so the HAOS-load tuning behind the
# 30 s resolve budget does not apply here.
HACS_ADD_REGISTRATION_TIMEOUT = 10.0

# Wall-clock budget for ``_resolve_hacs_repo_id`` (the ``owner/repo`` lookup
# behind read-only ``ha_get_hacs_info(action="info")`` and
# ``ha_manage_hacs(action="download")`` / ``(action="remove")``; a remove
# targets an already-installed repo, so for it the wait only ever burns
# wall-clock on a typo'd name — the same 10 s bound caps that stall).
# A plain info/download lookup targets a
# repo that should ALREADY be in HACS's index: default repos always are, and
# ``ha_manage_hacs(action="add_repository")`` blocks until registration is
# confirmed before returning (within its own ``HACS_ADD_REGISTRATION_TIMEOUT``
# budget).
# So the post-subscribe sample resolves an existing repo instantly, and the
# dispatch-signal wait only ever burns wall-clock when the repo is genuinely
# absent. The old 30 s budget made every not-found lookup a 30 s stall (the
# dominant cost in the HAOS E2E suite per #1515); 10 s still leaves event-driven
# headroom for the rare mid-registration race while failing a genuinely-missing
# lookup ~3x faster.
HACS_RESOLVE_REGISTRATION_TIMEOUT = 10.0


async def _find_repo_in_list_by_full_name(
    ws_client: Any, full_name_lower: str
) -> dict[str, Any] | None:
    """Return the HACS repo entry matching ``full_name_lower``, or None."""
    list_response = await ws_client.send_command("hacs/repositories/list")
    for repo in list_response.get("result", []):
        if repo.get("full_name", "").lower() == full_name_lower:
            # ``ws_client`` is ``Any`` so mypy can't narrow the result
            # entry. The HACS wire shape (``custom_components/hacs/
            # websocket/repositories.py``) always emits a dict per repo,
            # so a runtime guard would be defensive only.
            return dict(repo)
    return None


async def _last_chance_lookup_after_shutdown(
    ws_client: Any, full_name_lower: str
) -> dict[str, Any] | None:
    """One list lookup after a queue shutdown, swallowing transport errors.

    The teardown that shut the queue probably killed the WS, so the list
    call itself may fail; report "not found" rather than leaking a
    connection error out of what callers see as a wait timeout.
    """
    try:
        return await _find_repo_in_list_by_full_name(ws_client, full_name_lower)
    except (
        HomeAssistantConnectionError,
        HomeAssistantCommandError,
        OSError,
    ) as last_e:
        logger.debug(
            "Last-chance list lookup after queue shutdown failed: %s",
            last_e,
        )
        return None


async def _repo_from_matching_dispatch(
    ws_client: Any, event: dict[str, Any], full_name: str, full_name_lower: str
) -> dict[str, Any] | None:
    """Resolve a dispatch event to the full repo entry if it targets our repo.

    Returns the repo dict when the event is for ``full_name`` and the
    follow-up list lookup succeeds, else ``None`` (unrelated dispatch,
    no-payload nudge, or a lookup that raced the registration).
    """
    # HACS dispatch payload shape:
    #   {"action": "registration"|"install"|"uninstall",
    #    "repository": <full_name>, "repository_id": <id>}
    # Older/empty dispatches may send ``{}`` or ``None``; accept those.
    payload = event.get("event") or {}
    if not (
        isinstance(payload, dict)
        and payload.get("repository", "").lower() == full_name_lower
    ):
        return None

    # Matching repo dispatched — fetch the full entry since the event
    # payload only carries the three fields above and callers need more.
    repo = await _find_repo_in_list_by_full_name(ws_client, full_name_lower)
    if repo is not None:
        logger.info(f"Found {full_name} -> id={repo.get('id')} (HACS dispatch event)")
    return repo


async def _poll_queue_for_registration(
    ws_client: Any,
    queue: Any,
    full_name: str,
    full_name_lower: str,
    timeout: float,
    backstop_poll_interval: float,
) -> dict[str, Any] | None:
    """Wait on the subscription queue for our repo, with a wall-clock backstop.

    Assumes the caller already subscribed and did the post-subscribe
    sample. Returns the repo dict once seen, or ``None`` on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Operator-visible breadcrumb: a None return here means
            # we subscribed successfully but the event-driven path
            # didn't surface the repo within the budget. Different
            # signature from "subscribe failed" or "matching event
            # for wrong repo" — useful when diagnosing flakes.
            logger.warning(
                "wait_for_repo_registration timed out for %s after %.1fs",
                full_name,
                timeout,
            )
            return None

        # Wait for HACS to nudge us; if it doesn't, fall through
        # to a re-check at ``backstop_poll_interval``. Distinguish
        # "true backstop tick fired" from "wall-clock budget about
        # to exhaust" — the latter must NOT trigger an extra list
        # call right before the next iteration would exit anyway.
        wait_for = min(remaining, backstop_poll_interval)
        was_backstop_tick = remaining >= backstop_poll_interval
        try:
            event = await asyncio.wait_for(queue.get(), timeout=wait_for)
        except TimeoutError:
            event = None
        except asyncio.QueueShutDown:
            return await _last_chance_lookup_after_shutdown(ws_client, full_name_lower)

        if event is not None:
            repo = await _repo_from_matching_dispatch(
                ws_client, event, full_name, full_name_lower
            )
            if repo is not None:
                return repo
            # Unrelated dispatch, no-payload nudge, or a lookup that
            # raced the registration — go back to waiting. Re-listing
            # on every dispatch would defeat using the dispatcher as
            # the signal (HACS' list payload is 2 MB+ on busy installs);
            # we only re-list on the backstop tick.
            continue

        # event is None.
        if not was_backstop_tick:
            # The wait timed out because the wall-clock budget was about
            # to exhaust, not because the backstop cadence fired — loop
            # and let ``remaining <= 0`` exit cleanly without a list call.
            continue

        # True backstop poll: HACS dispatcher has been quiet for
        # ``backstop_poll_interval``. Belt-and-braces re-check the list
        # in case HACS dropped/lost a dispatch event.
        repo = await _find_repo_in_list_by_full_name(ws_client, full_name_lower)
        if repo is not None:
            logger.info(
                f"Found {full_name} -> id={repo.get('id')} (backstop poll sample)"
            )
            return repo


async def wait_for_repo_registration(
    ws_client: Any,
    full_name: str,
    *,
    timeout: float,
    backstop_poll_interval: float = HACS_REPO_BACKSTOP_POLL_INTERVAL,
) -> dict[str, Any] | None:
    """Wait for a HACS repo to register, using HACS' dispatch signal.

    Replaces the previous fixed 10x1s blind poll of
    ``hacs/repositories/list``. HACS dispatches
    ``HacsDispatchEvent.REPOSITORY`` whenever a repository entry
    registers / installs / mutates, exposed over the WebSocket via
    ``hacs/subscribe`` with a ``signal`` field. We subscribe before
    any wait, do a single post-subscribe sample to close the race
    with the preceding ``hacs/repositories/add``, then wait on the
    subscription queue with a wall-clock backstop.

    Args:
        ws_client: Connected HA WebSocket client.
        full_name: Repository full name in ``owner/repo`` form (case-insensitive).
        timeout: Wall-clock budget before giving up.
        backstop_poll_interval: Between dispatch nudges, re-check the
            list at this cadence to recover from a missed/lossy dispatch.

    Returns the HACS repo dict if found, or ``None`` on timeout.
    """
    full_name_lower = full_name.lower()

    # Narrow exception list: transport / command / timeout / socket
    # errors degrade to the polling fallback; programming bugs
    # (``AttributeError``, ``TypeError``, ``KeyError``) must propagate
    # so the underlying defect surfaces instead of being silently
    # masked as "HACS subscribe blew up" and a quiet degradation.
    try:
        sub_id, queue = await ws_client.subscribe_command(
            "hacs/subscribe",
            timeout=HACS_SUBSCRIBE_TIMEOUT,
            signal=HACS_REPOSITORY_SIGNAL,
        )
    except (
        HomeAssistantConnectionError,
        HomeAssistantCommandError,
        TimeoutError,
        OSError,
    ) as e:
        logger.warning(
            "hacs/subscribe failed (%s); falling back to single list lookup", e
        )
        return await _find_repo_in_list_by_full_name(ws_client, full_name_lower)

    try:
        # Two races to close around the preceding ``hacs/repositories/add``:
        #
        # (A) HACS finished registration BEFORE we sent the subscribe —
        #     no dispatch event will be delivered to us. This single
        #     post-subscribe list check catches that.
        # (B) HACS dispatches REPOSITORY DURING our subscribe-ack
        #     window — closed by ``subscribe_command`` registering the
        #     queue BEFORE calling ``send_json_message`` (so the event
        #     lands in the queue, not nowhere). Do NOT move that
        #     registration after the ack-wait — the sample below only
        #     covers case (A) and would let (B) regress silently.
        repo = await _find_repo_in_list_by_full_name(ws_client, full_name_lower)
        if repo is not None:
            logger.info(
                f"Found {full_name} -> id={repo.get('id')} (post-subscribe sample)"
            )
            return repo

        return await _poll_queue_for_registration(
            ws_client,
            queue,
            full_name,
            full_name_lower,
            timeout,
            backstop_poll_interval,
        )
    finally:
        # ``asyncio.shield`` so a cancellation of the surrounding task
        # (caller timed out, server torn down) does not also cancel the
        # HA-side ``unsubscribe_events`` mid-flight — that would leak
        # the subscription registration on HA's connection map.
        try:
            await asyncio.shield(ws_client.unsubscribe_command(sub_id))
        except asyncio.CancelledError:
            # Surrounding task is being cancelled. The shielded
            # unsubscribe has already been dispatched; allow the
            # cancellation to propagate.
            raise
