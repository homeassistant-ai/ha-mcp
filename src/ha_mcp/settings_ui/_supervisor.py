"""Supervisor add-on options + self-restart helpers for the settings UI.

Groups the two Supervisor concerns the settings handlers depend on:

- **Options merge/post** (``_supervisor_fetch_current_options`` /
  ``_supervisor_merge_and_post_options``) — Supervisor's
  ``/addons/self/options`` POST is a *full* replacement validated against
  the addon schema, so a partial edit must be merged into the current
  options before posting.
- **Self-restart** (``_schedule_supervisor_self_restart``) — a
  fire-and-forget ``/addons/self/restart`` POST scheduled after the
  response has flushed, so the browser isn't served a 5xx by the ingress
  proxy when Supervisor kills the container mid-response.

Leaf module (no imports from the settings_ui package) so the handler
families and ``__init__`` can depend on it without cycles.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, NamedTuple

import httpx

from ..client.supervisor_client import make_supervisor_httpx_client

logger = logging.getLogger(__name__)


class _SupervisorOptionsError(NamedTuple):
    """Discriminated failure shape for the supervisor options helpers.

    Two distinct failure classes need different recovery paths in the UI:

    - ``kind="transport"``: network / DNS / Supervisor unreachable / token
      missing. The route maps this to :class:`ErrorCode.CONNECTION_FAILED`
      so the UI surfaces the "is HA running, check connectivity"
      suggestions. ``status_code`` is always ``502`` for this kind.
    - ``kind="validation"``: Supervisor accepted the request but rejected
      the body against the addon schema (e.g. an unknown key, a missing
      required field). The route maps this to
      :class:`ErrorCode.CONFIG_VALIDATION_FAILED` and forwards the
      supervisor ``status_code`` verbatim so the UI shows a real 4xx and
      surfaces the schema-recovery suggestions.

    Collapsing both into a single string return (the previous shape)
    sent transport failures down the wrong recovery path. See
    PR #1420's code review for the motivation.
    """

    kind: Literal["transport", "validation"]
    message: str
    status_code: int

    @classmethod
    def transport(cls, message: str) -> _SupervisorOptionsError:
        """Build a transport-class error (always HTTP 502 upstream)."""
        return cls(kind="transport", message=message, status_code=502)

    @classmethod
    def validation(cls, message: str, status_code: int) -> _SupervisorOptionsError:
        """Build a validation-class error preserving supervisor's status code."""
        return cls(kind="validation", message=message, status_code=status_code)


async def _supervisor_fetch_current_options(
    verify_ssl: bool,
) -> tuple[dict[str, Any], _SupervisorOptionsError | None]:
    """GET ``/addons/self/info`` and return the current options dict.

    Supervisor's ``/addons/self/options`` POST is a *full* replacement
    validated against the addon schema — every required key must be
    present in the body. We can't ship a partial PATCH of just the
    fields the user changed, so callers must merge their changes into
    the full current options before posting. Mirrors the pattern in
    ``homeassistant-addon/start.py::maybe_persist_secret_path`` which
    spreads existing config (``{**config, "secret_path": secret_path}``)
    before calling ``persist_addon_options``.

    Returns ``(options_dict, error)`` where ``error`` is a
    :class:`_SupervisorOptionsError` carrying ``kind="transport"`` for
    network / token / non-JSON failures (mapped to ``CONNECTION_FAILED``
    upstream) and ``kind="validation"`` for supervisor ``>=400``
    responses (mapped to ``CONFIG_VALIDATION_FAILED`` with supervisor's
    real status code preserved). On success the dict carries the
    full options and ``error`` is ``None``.
    """
    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=verify_ssl
        ) as sclient:
            resp = await sclient.get("/addons/self/info")
    except RuntimeError as exc:
        # `make_supervisor_httpx_client` raises RuntimeError when
        # SUPERVISOR_TOKEN is unset. Both current callers gate on that
        # env var, but treat this as transport (env / setup failure) so
        # a future third caller missing the gate gets a sane 502 rather
        # than an uncaught 500.
        return {}, _SupervisorOptionsError.transport(
            f"Supervisor client unavailable: {exc}"
        )
    except httpx.HTTPError as exc:
        return {}, _SupervisorOptionsError.transport(
            f"Could not reach Supervisor for current options: {exc}"
        )
    if resp.status_code >= 400:
        # Supervisor returning a 4xx/5xx for /info is itself a transport-
        # class failure (we never sent body — there is no schema for
        # the GET to validate). 502 with CONNECTION_FAILED is right.
        return {}, _SupervisorOptionsError.transport(
            f"Supervisor returned {resp.status_code} for "
            f"/addons/self/info: {resp.text[:300]}"
        )
    try:
        body = resp.json()
    except ValueError:
        return {}, _SupervisorOptionsError.transport(
            "Supervisor returned non-JSON for /addons/self/info"
        )
    # Supervisor REST envelope is {"result": "ok", "data": {...}}. Older
    # mocks / variants may return the data dict directly — handle both.
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    if not isinstance(data, dict):
        return {}, _SupervisorOptionsError.transport(
            "Supervisor /addons/self/info had non-object body"
        )
    options = data.get("options")
    if not isinstance(options, dict):
        return {}, _SupervisorOptionsError.transport(
            "Supervisor /addons/self/info had no options dict"
        )
    return options, None


async def _supervisor_merge_and_post_options(
    verify_ssl: bool, field_changes: dict[str, Any]
) -> tuple[bool, _SupervisorOptionsError | None]:
    """Merge ``field_changes`` into supervisor's current options and POST.

    Necessary because supervisor's POST is full-replacement (see
    :func:`_supervisor_fetch_current_options`). Without this merge, a
    POST that only includes a handful of fields the user edited
    drops every other key (including required ones like ``backup_hint``)
    and supervisor rejects with a 400 ``addon_configuration_invalid_error``.

    Returns ``(success, error)`` where ``error`` is a
    :class:`_SupervisorOptionsError`. Transport failures (token missing,
    network drop, malformed response from /info) bubble up from the
    fetch helper unchanged. Supervisor 4xx on the actual POST is
    classified as ``kind="validation"`` with supervisor's status code
    preserved so the UI can show the real 4xx code and the
    ``CONFIG_VALIDATION_FAILED`` recovery suggestions.
    """
    current, err = await _supervisor_fetch_current_options(verify_ssl)
    if err is not None:
        return False, err
    merged = {**current, **field_changes}
    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=verify_ssl
        ) as sclient:
            resp = await sclient.post("/addons/self/options", json={"options": merged})
    except RuntimeError as exc:
        return False, _SupervisorOptionsError.transport(
            f"Supervisor client unavailable: {exc}"
        )
    except httpx.HTTPError as exc:
        return False, _SupervisorOptionsError.transport(
            f"Supervisor options POST failed: {exc}"
        )
    if resp.status_code >= 400:
        return False, _SupervisorOptionsError.validation(
            (
                f"Supervisor rejected options update ({resp.status_code}): "
                f"{resp.text[:400]}"
            ),
            resp.status_code,
        )
    return True, None


# Strong references to in-flight self-restart tasks, kept here so the
# event loop's weakref-only task table doesn't garbage-collect a still-
# running fire-and-forget coroutine before it can POST to supervisor.
# Tasks remove themselves via ``add_done_callback`` when they finish.
_BACKGROUND_RESTART_TASKS: set[asyncio.Task[None]] = set()


# Delay (seconds) before the background self-restart task fires the
# supervisor POST. Picked to give Starlette + uvicorn time to serialize
# the JSONResponse onto the socket and have HA ingress flush it to the
# browser BEFORE supervisor kills the addon container. Too short races
# the response flush (browser sees a 5xx Bad Gateway from ingress); too
# long delays the visible restart noticeably. 0.3s is comfortably above
# observed flush times in addon-mode while staying well under any
# reasonable user attention threshold. Tests override via the ``delay``
# kwarg of ``_schedule_supervisor_self_restart``.
_SUPERVISOR_SELF_RESTART_FLUSH_DELAY_S: float = 0.3


def _schedule_supervisor_self_restart(
    verify_ssl: bool, *, delay: float = _SUPERVISOR_SELF_RESTART_FLUSH_DELAY_S
) -> None:
    """Schedule a background ``/addons/self/restart`` POST.

    Fire-and-forget on the current event loop so the request handler
    can return its JSON response *before* the supervisor kills the
    addon. Without the gap, supervisor restarts our process mid-response
    and the HA ingress proxy converts the dropped upstream connection
    into a 5xx Bad Gateway, which the browser interprets as "Restart
    failed" even though the restart actually succeeded.

    The ``delay`` (default 0.3s) gives Starlette + uvicorn time to
    serialize the JSONResponse onto the socket and have ingress flush
    it to the browser before the background coroutine wakes up and
    POSTs the supervisor restart. Tuned conservatively — too short
    races the response flush; too long delays the user-visible
    restart noticeably.

    Errors are logged and swallowed: by the time this fires the
    response has already gone out and the user has already been told
    the restart is initiated, so there is no path to surface a late
    failure here. The user discovers a failed restart by the addon
    not actually restarting; the supervisor log captures the cause.
    """

    async def _do_restart() -> None:
        await asyncio.sleep(delay)
        try:
            async with make_supervisor_httpx_client(
                timeout=5.0, verify=verify_ssl
            ) as sclient:
                resp = await sclient.post("/addons/self/restart")
            if resp.status_code >= 400:
                logger.error(
                    "Background self-restart returned %d: %s",
                    resp.status_code,
                    resp.text[:500],
                )
        except (httpx.ReadError, httpx.RemoteProtocolError):
            # Supervisor killed us mid-call — expected; no action needed.
            pass
        except RuntimeError:
            # ``make_supervisor_httpx_client`` raises RuntimeError when
            # SUPERVISOR_TOKEN is unset. The route guard at handler entry
            # already checks for this, but a race that unsets the token
            # between request entry and the 300ms-later task wakeup
            # would otherwise propagate uncaught and surface only as
            # asyncio's "Task exception was never retrieved" at GC time.
            # Log it loudly so the user can find it in the addon log.
            # Mirrors the same RuntimeError catch in the supervisor
            # options helpers.
            logger.exception("Background self-restart aborted: SUPERVISOR_TOKEN unset")
        except httpx.HTTPError:
            logger.exception("Background self-restart failed")

    task = asyncio.create_task(_do_restart())
    _BACKGROUND_RESTART_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_RESTART_TASKS.discard)
