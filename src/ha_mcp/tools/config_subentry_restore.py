"""Restore a config subentry from a backup snapshot.

An existing subentry goes through its reconfigure flow, a deleted one through
its create flow; either is filled form by form as an options restore is
(:class:`_OptionsFlowProgress`). Home Assistant ends a successful reconfigure
with a ``reconfigure_successful`` abort instead of ``create_entry``.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from .config_entry_flow import (
    _SUBENTRY_CREATE_LOCKS,
    OptionsFlowError,
    _abort_subentry_flow_best_effort,
    _created_subentry_id,
    _OptionsFlowProgress,
    _reject_redaction_sentinels,
    _subentry_ids,
)
from .config_entry_flow_walker import (
    _RECONFIGURE_SUCCESS_REASONS,
    _FlowType,
    _handle_flow_steps,
)


class _SubentryFlowProgress(_OptionsFlowProgress):
    """Track a subentry flow, whose reconfigure success reply is an abort."""

    operation = "Subentry restore"

    def record_reply(self, result: dict[str, Any]) -> None:
        if (
            result.get("type") == _FlowType.ABORT
            and result.get("reason") in _RECONFIGURE_SUCCESS_REASONS
        ):
            self.current_step = result
            self.apply_status = "applied"
            return
        super().record_reply(result)

    async def submit(
        self, client: Any, flow_id: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return await super().submit(
            client, flow_id, payload, submit_fn=client.submit_config_subentry_flow_step
        )


async def _walk_snapshot(
    client: Any,
    progress: _SubentryFlowProgress,
    subentry_type: str,
    subentry_id: str | None,
) -> None:
    data = progress.config
    try:
        _reject_redaction_sentinels(data)
        initial_step = await client.start_config_subentry_flow(
            progress.entry_id, subentry_type, subentry_id=subentry_id
        )
        progress.flow_id = initial_step.get("flow_id")
        progress.start(initial_step)
        await _handle_flow_steps(
            client,
            progress.flow_id or "",
            initial_step,
            dict(data),
            submit_fn=partial(progress.submit, client),
            is_reconfigure=subentry_id is not None,
            complete_snapshot=True,
        )
    except (Exception, asyncio.CancelledError) as err:
        failure = err if isinstance(err, OptionsFlowError) else progress.failure()
        if failure.flow_id and failure.apply_status == "not_applied":
            await _abort_subentry_flow_best_effort(client, failure.flow_id)
        if isinstance(err, (OptionsFlowError, asyncio.CancelledError)):
            raise
        raise failure from err


async def restore_config_subentry(
    client: Any,
    entry_id: str,
    subentry_id: str,
    subentry_type: str,
    data: dict[str, Any],
    stored: dict[str, Any],
) -> dict[str, Any]:
    """Reconfigure the subentry to ``data``; ``stored`` is its current data.

    A snapshot field none of the reconfigure forms offers must still equal the
    stored value. When Home Assistant marks the last form (``last_step``) that
    is checked before anything is submitted; a flow that marks none (most
    subentry flows) is applied and verified by readback.
    Raises :class:`OptionsFlowError` with apply knowledge on failure.
    """
    progress = _SubentryFlowProgress(entry_id, config=dict(data), fixed=stored)
    await _walk_snapshot(client, progress, subentry_type, subentry_id)
    return {"success": True, "entry_id": entry_id, "subentry_id": subentry_id}


async def recreate_config_subentry(
    client: Any, entry_id: str, subentry_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Create a deleted subentry again from its data; a field no form takes is
    refused before a form Home Assistant marks as last."""
    progress = _SubentryFlowProgress(entry_id, config=dict(data), fixed={})
    async with _SUBENTRY_CREATE_LOCKS[entry_id]:
        before = await _subentry_ids(client, entry_id)
        if before is None:
            # Without the listing the new subentry could not be identified.
            raise progress.failure()
        await _walk_snapshot(client, progress, subentry_type, None)
        subentry_id = await _created_subentry_id(client, entry_id, before)
    if subentry_id is None:
        raise OptionsFlowError(
            "Subentry recreation returned no new identity",
            apply_status="applied",
            entry_id=entry_id,
            flow_id=progress.flow_id,
        )
    return {
        "success": True,
        "entry_id": entry_id,
        "subentry_id": subentry_id,
        "restore_mode": "recreated",
    }
