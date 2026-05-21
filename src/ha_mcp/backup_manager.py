"""Auto-backup of HA config domains before write/destructive operations.

Closes #1288. Captures per-entity pre-write state to a local directory.
Best-effort: failures log a WARNING but never block the underlying write.

Storage path resolution
-----------------------
``Settings.auto_backup_dir`` overrides; otherwise defaults to
``/data/ha_mcp_backups`` in the add-on (SUPERVISOR_TOKEN set + ``/data``
exists), else ``${XDG_DATA_HOME:-~/.local/share}/ha_mcp/backups``.

File format
-----------
One YAML file per snapshot, named
``<domain>.<safe_entity_id>.<YYYYMMDD_HHMMSS>.yaml``::

    # ha_mcp_backup
    schema_version: 1
    domain: automation
    entity_id: kitchen_lights
    captured: 2026-05-21T15:30:00+00:00
    tool: ha_config_set_automation
    config:
      alias: Kitchen lights
      trigger: ...

Domain handlers
---------------
``DomainHandler`` pairs a backup-domain string with two coroutines:

- ``fetch(client, entity_id) -> config dict``  — read pre-write state
- ``restore(client, entity_id, config) -> result`` — re-apply the saved state

One handler per backed-up domain. Helper types each register their own
handler keyed ``helper_<type>`` since each helper type has a distinct
WS endpoint shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")

# Filename pattern: <domain>.<safe_entity_id>.<YYYYMMDD_HHMMSS>.yaml
# The middle ``.`` separators make the timestamp rsplit reliable even
# when entity_id contains dots (after sanitization, dots are kept).
_FILENAME_RE = re.compile(
    r"^(?P<domain>[A-Za-z0-9_]+)\."
    r"(?P<entity_id>[A-Za-z0-9._-]+)\."
    r"(?P<ts>\d{8}_\d{6})\.yaml$"
)


# ----------------------------- handler protocol -----------------------------

FetchFn = Callable[[Any, str], Awaitable[Any]]
RestoreFn = Callable[[Any, str, Any], Awaitable[Any]]


@dataclass(frozen=True)
class DomainHandler:
    """Per-domain fetch + restore pair.

    ``domain`` is the backup-domain key — exactly what the decorator
    passes as ``domain=`` and what gets baked into snapshot filenames.
    """

    domain: str
    fetch: FetchFn
    restore: RestoreFn


# ----------------------------- backup manager -------------------------------


def _safe_entity_id(entity_id: str) -> str:
    """Sanitize an entity id for use in a filename.

    Replaces any character outside ``[A-Za-z0-9._-]`` with ``_``. Path
    separators get caught by this (the regex excludes both ``/`` and ``\\``).
    Strips leading dots to prevent dotfile collisions.
    """
    if not entity_id:
        return "_"
    cleaned = _SAFE_ID_RE.sub("_", entity_id).lstrip(".")
    return cleaned or "_"


def _now_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_default_dir() -> Path:
    """Pick a sane default backup directory for the current deployment mode."""
    # Addon detection: Supervisor sets SUPERVISOR_TOKEN AND /data exists.
    if os.environ.get("SUPERVISOR_TOKEN") and Path("/data").is_dir():
        return Path("/data/ha_mcp_backups")
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "ha_mcp" / "backups"
    return (Path.home() / ".local" / "share" / "ha_mcp" / "backups").resolve()


class BackupManager:
    """Per-entity snapshot manager. One instance per server, cached on client."""

    def __init__(self, settings: Any, client: Any) -> None:
        self._settings = settings
        self._client = client
        self._handlers: dict[str, DomainHandler] = {}
        self._last_snapshot: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._init_dir_error: str | None = None
        self._dir = self._resolve_dir()

    # ----- configuration -------------------------------------------------

    def _resolve_dir(self) -> Path:
        configured = (getattr(self._settings, "auto_backup_dir", "") or "").strip()
        path = Path(configured).expanduser() if configured else _resolve_default_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self._init_dir_error = f"{type(err).__name__}: {err}"
            logger.warning(
                "Auto-backup: could not create backup dir %s: %s. "
                "Captures will be silently skipped until startup is rerun.",
                path,
                err,
            )
        return path

    @property
    def backup_dir(self) -> Path:
        return self._dir

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "enable_auto_backup", False))

    @property
    def throttle_seconds(self) -> int:
        return max(
            0, int(getattr(self._settings, "auto_backup_throttle_minutes", 0)) * 60
        )

    @property
    def retain_per_entity(self) -> int:
        return max(1, int(getattr(self._settings, "auto_backup_retain_per_entity", 20)))

    # ----- handler registration ------------------------------------------

    def register(self, handler: DomainHandler) -> None:
        self._handlers[handler.domain] = handler

    def handler_for(self, domain: str) -> DomainHandler | None:
        return self._handlers.get(domain)

    # ----- capture -------------------------------------------------------

    async def maybe_snapshot(
        self,
        domain: str,
        entity_id: str,
        *,
        tool_name: str | None = None,
    ) -> Path | None:
        """Capture a snapshot for ``domain:entity_id`` if throttle elapsed.

        Returns the Path written or None if skipped. Never raises — all
        errors are logged at WARNING and swallowed so the wrapped write
        can proceed regardless.
        """
        if not self.enabled or self._init_dir_error is not None:
            return None
        if not entity_id:
            # Create-mode call with no ID yet — nothing to back up.
            return None
        handler = self._handlers.get(domain)
        if handler is None:
            logger.warning(
                "Auto-backup: no handler registered for domain %r — skipping",
                domain,
            )
            return None

        key = f"{domain}:{entity_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            throttle = self.throttle_seconds
            if throttle and (now - self._last_snapshot.get(key, 0.0)) < throttle:
                return None
            try:
                config = await handler.fetch(self._client, entity_id)
            except Exception as err:
                logger.warning(
                    "Auto-backup: fetch failed for %s — %s: %s",
                    key,
                    type(err).__name__,
                    err,
                )
                return None
            if config is None:
                # Entity didn't exist at fetch time (create operation, or
                # already-deleted at remove time before our pre-fetch).
                return None
            try:
                path = await asyncio.to_thread(
                    self._write_snapshot, domain, entity_id, config, tool_name
                )
            except Exception as err:
                logger.warning(
                    "Auto-backup: write failed for %s — %s: %s",
                    key,
                    type(err).__name__,
                    err,
                )
                return None
            self._last_snapshot[key] = now
            try:
                await asyncio.to_thread(self._rotate, domain, entity_id)
            except Exception as err:
                logger.warning(
                    "Auto-backup: rotation failed for %s — %s: %s",
                    key,
                    type(err).__name__,
                    err,
                )
            return path

    def _write_snapshot(
        self, domain: str, entity_id: str, config: Any, tool_name: str | None
    ) -> Path:
        safe = _safe_entity_id(entity_id)
        ts = _now_ts()
        filename = f"{domain}.{safe}.{ts}.yaml"
        target = self._dir / filename
        payload = {
            "schema_version": SCHEMA_VERSION,
            "domain": domain,
            "entity_id": entity_id,
            "captured": _now_iso(),
            "tool": tool_name,
            "config": config,
        }
        body = yaml.safe_dump(payload, default_flow_style=False, sort_keys=False)
        # Atomic write via tmp+rename
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text("# ha_mcp_backup\n" + body)
        os.replace(str(tmp), str(target))
        logger.info("Auto-backup: wrote %s", target.name)
        return target

    def _rotate(self, domain: str, entity_id: str) -> None:
        safe = _safe_entity_id(entity_id)
        pattern = f"{domain}.{safe}.*.yaml"
        files = sorted(self._dir.glob(pattern))
        excess = len(files) - self.retain_per_entity
        for old in files[: max(0, excess)]:
            try:
                old.unlink()
            except OSError as err:
                logger.warning("Auto-backup: failed to rotate %s: %s", old.name, err)

    # ----- list / read / delete ------------------------------------------

    def list_snapshots(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._dir.exists():
            return []
        out: list[dict[str, Any]] = []
        # Reverse-sorted glob — newest filenames sort last lexicographically,
        # so reverse=True yields newest-first.
        for path in sorted(self._dir.glob("*.yaml"), reverse=True):
            meta = self._parse_filename(path.name)
            if meta is None:
                continue
            if domain and meta["domain"] != domain:
                continue
            if entity_id and meta["entity_id"] != entity_id:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            meta["size"] = stat.st_size
            meta["mtime"] = stat.st_mtime
            out.append(meta)
            if limit and len(out) >= limit:
                break
        return out

    def _parse_filename(self, name: str) -> dict[str, Any] | None:
        m = _FILENAME_RE.match(name)
        if m is None:
            return None
        return {
            "name": name,
            "domain": m.group("domain"),
            "entity_id": m.group("entity_id"),
            "timestamp": m.group("ts"),
        }

    def read_snapshot(self, name: str) -> dict[str, Any]:
        path = self._resolve_snapshot_path(name)
        text = path.read_text()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as err:
            raise ValueError(f"Snapshot {name!r} is not valid YAML: {err}") from err
        if not isinstance(data, dict):
            raise ValueError(f"Snapshot {name!r} is not a YAML mapping")
        sv = data.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise ValueError(
                f"Snapshot {name!r} has unsupported schema_version={sv!r} "
                f"(expected {SCHEMA_VERSION})"
            )
        return data

    def delete_snapshot(self, name: str) -> Path:
        path = self._resolve_snapshot_path(name)
        path.unlink()
        return path

    def delete_bulk(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        older_than_days: int | None = None,
    ) -> list[str]:
        deleted: list[str] = []
        cutoff: float | None = None
        if older_than_days is not None:
            if older_than_days < 0:
                raise ValueError("older_than_days must be >= 0")
            cutoff = time.time() - (older_than_days * 86400)
        for meta in self.list_snapshots(domain=domain, entity_id=entity_id):
            if cutoff is not None and meta["mtime"] >= cutoff:
                continue
            try:
                (self._dir / meta["name"]).unlink()
                deleted.append(meta["name"])
            except OSError as err:
                logger.warning(
                    "Auto-backup: bulk-delete failed for %s: %s", meta["name"], err
                )
        return deleted

    def _resolve_snapshot_path(self, name: str) -> Path:
        """Validate a snapshot name and return its absolute Path.

        Rejects any name that contains path separators or escapes the
        backup directory.
        """
        if not name or os.sep in name or "/" in name or ".." in name:
            raise ValueError(f"Invalid snapshot name: {name!r}")
        path = (self._dir / name).resolve()
        # Defence-in-depth: post-resolve, verify still under backup_dir.
        try:
            path.relative_to(self._dir.resolve())
        except ValueError as err:
            raise ValueError(f"Invalid snapshot name: {name!r}") from err
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    # ----- restore -------------------------------------------------------

    async def restore_snapshot(
        self, name: str, *, take_safety_backup: bool = True
    ) -> dict[str, Any]:
        data = self.read_snapshot(name)
        domain = data["domain"]
        entity_id = data["entity_id"]
        config = data["config"]
        handler = self._handlers.get(domain)
        if handler is None:
            raise LookupError(f"No restore handler registered for domain {domain!r}")

        safety_path: Path | None = None
        if take_safety_backup:
            safety_path = await self.maybe_snapshot(
                domain, entity_id, tool_name="ha_manage_backup.restore.safety"
            )
        result = await handler.restore(self._client, entity_id, config)
        return {
            "restored_from": name,
            "domain": domain,
            "entity_id": entity_id,
            "safety_backup": safety_path.name if safety_path else None,
            "result": result,
        }


# --------------------------- attach to client -------------------------------


def get_backup_manager(client: Any, settings: Any) -> BackupManager:
    """Get-or-create the singleton BackupManager attached to ``client``.

    Stored on the client object so tools that share a client share one
    manager (and one set of per-entity locks).
    """
    mgr = getattr(client, "_auto_backup_manager", None)
    if mgr is None:
        mgr = BackupManager(settings, client)
        register_default_handlers(mgr, client)
        try:
            client._auto_backup_manager = mgr
        except (AttributeError, TypeError):
            # Read-only client (e.g. a slotted mock) — manager still works,
            # just isn't cached.
            pass
    return mgr


# --------------------------- domain handlers --------------------------------
#
# Fetchers READ the entity's current config; restorers WRITE it back.
# Each pair calls the same HA endpoint that the underlying ``ha_config_set_*``
# / ``ha_config_remove_*`` tool already uses, so restore re-applies cleanly.


async def _rest_get_or_none(client: Any, path: str) -> Any:
    """Fetch a REST path; return None on 404, propagate other errors."""
    try:
        return await client.get(path)
    except Exception as err:
        # Detect 404 from the HomeAssistantAPIError shape used by rest_client.
        status = getattr(err, "status_code", None)
        if status == 404:
            return None
        raise


async def _rest_post(client: Any, path: str, payload: Any) -> Any:
    return await client.post(path, json_data=payload)


async def _ws_send(client: Any, message: dict[str, Any]) -> Any:
    """Send a WS command using the same lazy-connect pattern as other tools.

    Builds a one-shot WS client each call. Latency is acceptable because
    captures are off the critical path (the wrapped write runs regardless)
    and are throttled per-entity by default.
    """
    # Import inside the function to avoid an import cycle:
    # backup_manager → tools.helpers → ... (tools depend on backup_manager).
    from .tools.helpers import get_connected_ws_client

    ws_client, error = await get_connected_ws_client(
        client.base_url, client.token, verify_ssl=client.verify_ssl
    )
    if error or ws_client is None:
        raise RuntimeError(
            (error or {}).get("error", {}).get("message", "WS connect failed")
        )
    try:
        cmd_type = message.pop("type")
        result = await ws_client.send_command(cmd_type, **message)
    finally:
        # Best-effort close; never raise from cleanup.
        try:
            await ws_client.disconnect()
        except Exception:
            pass
    return result


# Automation / Script / Scene share the /api/config/<domain>/config/<id> shape.


async def _fetch_automation(client: Any, entity_id: str) -> Any:
    eid = (
        entity_id[len("automation.") :]
        if entity_id.startswith("automation.")
        else entity_id
    )
    return await _rest_get_or_none(client, f"/api/config/automation/config/{eid}")


async def _restore_automation(client: Any, entity_id: str, config: Any) -> Any:
    eid = (
        entity_id[len("automation.") :]
        if entity_id.startswith("automation.")
        else entity_id
    )
    return await _rest_post(client, f"/api/config/automation/config/{eid}", config)


async def _fetch_script(client: Any, entity_id: str) -> Any:
    sid = entity_id[len("script.") :] if entity_id.startswith("script.") else entity_id
    return await _rest_get_or_none(client, f"/api/config/script/config/{sid}")


async def _restore_script(client: Any, entity_id: str, config: Any) -> Any:
    sid = entity_id[len("script.") :] if entity_id.startswith("script.") else entity_id
    return await _rest_post(client, f"/api/config/script/config/{sid}", config)


async def _fetch_scene(client: Any, entity_id: str) -> Any:
    sid = entity_id[len("scene.") :] if entity_id.startswith("scene.") else entity_id
    return await _rest_get_or_none(client, f"/api/config/scene/config/{sid}")


async def _restore_scene(client: Any, entity_id: str, config: Any) -> Any:
    sid = entity_id[len("scene.") :] if entity_id.startswith("scene.") else entity_id
    return await _rest_post(client, f"/api/config/scene/config/{sid}", config)


# Dashboards — WS lovelace/config (fetch) and lovelace/config/save (restore).


async def _fetch_dashboard(client: Any, entity_id: str) -> Any:
    try:
        return await _ws_send(
            client, {"type": "lovelace/config", "url_path": entity_id, "force": True}
        )
    except Exception as err:
        if "not_found" in str(err).lower() or "config_not_found" in str(err).lower():
            return None
        raise


async def _restore_dashboard(client: Any, entity_id: str, config: Any) -> Any:
    return await _ws_send(
        client,
        {
            "type": "lovelace/config/save",
            "url_path": entity_id,
            "config": config,
        },
    )


# Dashboard resources — WS lovelace_resources commands.


async def _fetch_dashboard_resource(client: Any, entity_id: str) -> Any:
    resources = await _ws_send(client, {"type": "lovelace/resources"})
    if not isinstance(resources, list):
        return None
    for res in resources:
        if str(res.get("id")) == entity_id:
            return res
    return None


async def _restore_dashboard_resource(client: Any, entity_id: str, config: Any) -> Any:
    payload = dict(config)
    payload["resource_id"] = entity_id
    payload["type"] = "lovelace/resources/update"
    return await _ws_send(client, payload)


# Labels — config/label_registry/{list,update}


async def _fetch_label(client: Any, entity_id: str) -> Any:
    items = await _ws_send(client, {"type": "config/label_registry/list"})
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("label_id") == entity_id:
            return item
    return None


async def _restore_label(client: Any, entity_id: str, config: Any) -> Any:
    payload = {k: v for k, v in config.items() if k != "label_id"}
    payload["type"] = "config/label_registry/update"
    payload["label_id"] = entity_id
    return await _ws_send(client, payload)


# Categories — config/category_registry/{list,update}


async def _fetch_category(client: Any, entity_id: str) -> Any:
    scope, _, cat_id = entity_id.partition(":")
    if not cat_id:
        return None
    items = await _ws_send(
        client, {"type": "config/category_registry/list", "scope": scope}
    )
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("category_id") == cat_id:
            return {"scope": scope, **item}
    return None


async def _restore_category(client: Any, entity_id: str, config: Any) -> Any:
    scope, _, cat_id = entity_id.partition(":")
    payload = {k: v for k, v in config.items() if k not in ("category_id", "scope")}
    payload["type"] = "config/category_registry/update"
    payload["scope"] = scope or config.get("scope")
    payload["category_id"] = cat_id
    return await _ws_send(client, payload)


# Groups — group.set service. Fetch via state API.


async def _fetch_group(client: Any, entity_id: str) -> Any:
    eid = entity_id if entity_id.startswith("group.") else f"group.{entity_id}"
    state = await _rest_get_or_none(client, f"/api/states/{eid}")
    if state is None:
        return None
    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    return {
        "object_id": eid.split(".", 1)[1],
        "name": attrs.get("friendly_name"),
        "entities": attrs.get("entity_id", []),
        "icon": attrs.get("icon"),
    }


async def _restore_group(client: Any, entity_id: str, config: Any) -> Any:
    object_id = config.get("object_id") or entity_id.split(".", 1)[-1]
    service_data: dict[str, Any] = {"object_id": object_id}
    if config.get("name"):
        service_data["name"] = config["name"]
    if config.get("entities"):
        service_data["entities"] = config["entities"]
    if config.get("icon"):
        service_data["icon"] = config["icon"]
    return await _rest_post(client, "/api/services/group/set", service_data)


# Calendar events — calendar.get_events to fetch, calendar.create/update services.


async def _fetch_calendar_event(client: Any, entity_id: str) -> Any:
    # entity_id is "<calendar.entity>::<event_uid>"
    cal, _, uid = entity_id.partition("::")
    if not cal or not uid:
        return None
    # 7-day lookahead window is wide enough to catch most edits.
    start = datetime.now(UTC).isoformat()
    payload = {
        "type": "execute_script",
        "sequence": [
            {
                "service": "calendar.get_events",
                "target": {"entity_id": cal},
                "data": {"duration": {"days": 7}, "start_date_time": start},
                "response_variable": "events",
            },
            {"stop": "", "response_variable": "events"},
        ],
    }
    try:
        result = await _ws_send(client, payload)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    events = result.get("response", {}).get("events", {}).get(cal, {}).get("events", [])
    for ev in events:
        if ev.get("uid") == uid:
            return {"calendar_entity_id": cal, **ev}
    return None


async def _restore_calendar_event(client: Any, entity_id: str, config: Any) -> Any:
    cal = config.get("calendar_entity_id")
    if not cal:
        cal = entity_id.split("::", 1)[0]
    data = {k: v for k, v in config.items() if k != "calendar_entity_id"}
    return await _rest_post(
        client,
        "/api/services/calendar/create_event",
        {"entity_id": cal, **data},
    )


# Zones — config/zone/{list,update}


async def _fetch_zone(client: Any, entity_id: str) -> Any:
    items = await _ws_send(client, {"type": "config/zone/list"})
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("id") == entity_id or item.get("name") == entity_id:
            return item
    return None


async def _restore_zone(client: Any, entity_id: str, config: Any) -> Any:
    payload = {k: v for k, v in config.items() if k != "id"}
    payload["type"] = "config/zone/update"
    payload["zone_id"] = config.get("id", entity_id)
    return await _ws_send(client, payload)


# Areas / floors — config/area_registry/{list,update}, config/floor_registry/{list,update}


async def _fetch_area_or_floor(client: Any, entity_id: str) -> Any:
    kind, _, real_id = entity_id.partition(":")
    if not real_id:
        return None
    if kind == "area":
        items = await _ws_send(client, {"type": "config/area_registry/list"})
        if not isinstance(items, list):
            return None
        for item in items:
            if item.get("area_id") == real_id:
                return {"kind": "area", **item}
    elif kind == "floor":
        items = await _ws_send(client, {"type": "config/floor_registry/list"})
        if not isinstance(items, list):
            return None
        for item in items:
            if item.get("floor_id") == real_id:
                return {"kind": "floor", **item}
    return None


async def _restore_area_or_floor(client: Any, entity_id: str, config: Any) -> Any:
    kind, _, real_id = entity_id.partition(":")
    payload = {
        k: v for k, v in config.items() if k not in ("kind", "area_id", "floor_id")
    }
    if kind == "area":
        payload["type"] = "config/area_registry/update"
        payload["area_id"] = real_id
    elif kind == "floor":
        payload["type"] = "config/floor_registry/update"
        payload["floor_id"] = real_id
    else:
        raise ValueError(f"Unknown area/floor kind: {kind!r}")
    return await _ws_send(client, payload)


# Todo items — entity_id is "<todo.entity>::<item_uid>"


async def _fetch_todo_item(client: Any, entity_id: str) -> Any:
    cal, _, uid = entity_id.partition("::")
    if not cal or not uid:
        return None
    payload = {
        "type": "execute_script",
        "sequence": [
            {
                "service": "todo.get_items",
                "target": {"entity_id": cal},
                "response_variable": "items",
            },
            {"stop": "", "response_variable": "items"},
        ],
    }
    try:
        result = await _ws_send(client, payload)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    items = result.get("response", {}).get("items", {}).get(cal, {}).get("items", [])
    for item in items:
        if item.get("uid") == uid:
            return {"todo_entity_id": cal, **item}
    return None


async def _restore_todo_item(client: Any, entity_id: str, config: Any) -> Any:
    cal = config.get("todo_entity_id") or entity_id.split("::", 1)[0]
    data = {k: v for k, v in config.items() if k != "todo_entity_id"}
    return await _rest_post(
        client,
        "/api/services/todo/add_item",
        {"entity_id": cal, **data},
    )


# Generic entity (ha_set_entity) — fetch via state API, restore by re-setting.


async def _fetch_entity_state(client: Any, entity_id: str) -> Any:
    return await _rest_get_or_none(client, f"/api/states/{entity_id}")


async def _restore_entity_state(client: Any, entity_id: str, config: Any) -> Any:
    # ha_set_entity just calls /api/states/<entity_id> POST with state+attributes.
    if isinstance(config, dict):
        payload = {
            "state": config.get("state"),
            "attributes": config.get("attributes", {}),
        }
    else:
        payload = {"state": str(config)}
    return await _rest_post(client, f"/api/states/{entity_id}", payload)


# Integration enable/disable — restore re-applies the disabled flag.


async def _fetch_integration(client: Any, entity_id: str) -> Any:
    items = await _ws_send(client, {"type": "config_entries/get"})
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("entry_id") == entity_id:
            return item
    return None


async def _restore_integration(client: Any, entity_id: str, config: Any) -> Any:
    disabled = config.get("disabled_by") is not None
    return await _ws_send(
        client,
        {
            "type": "config_entries/disable",
            "entry_id": entity_id,
            "disabled_by": "user" if disabled else None,
        },
    )


# Helpers — one handler family. Entity ID is "<helper_type>:<id>" so each
# helper type lists/restores via its native WS endpoints. The decorator
# constructs the domain key as ``helper_<type>`` so files group naturally.

_HELPER_LIST_TYPES = {
    "input_boolean",
    "input_text",
    "input_number",
    "input_select",
    "input_datetime",
    "input_button",
    "counter",
    "timer",
    "schedule",
}


async def _fetch_helper(client: Any, entity_id: str, helper_type: str) -> Any:
    if helper_type not in _HELPER_LIST_TYPES:
        # Other helper types (template, group, utility_meter, ...) live in
        # config entries; fall back to entity state.
        state = await _rest_get_or_none(client, f"/api/states/{entity_id}")
        if state is None:
            return None
        attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
        return {"object_id": entity_id.split(".", 1)[-1], **attrs}

    items = await _ws_send(client, {"type": f"{helper_type}/list"})
    if not isinstance(items, list):
        return None
    object_id = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    for item in items:
        if item.get("id") == object_id or item.get("id") == entity_id:
            return item
    return None


async def _restore_helper(
    client: Any, entity_id: str, config: Any, helper_type: str
) -> Any:
    if helper_type not in _HELPER_LIST_TYPES:
        # Best-effort: try restoring entity state.
        return await _restore_entity_state(client, entity_id, config)
    payload = {k: v for k, v in config.items() if k != "id"}
    payload["type"] = f"{helper_type}/update"
    payload[f"{helper_type}_id"] = config.get("id", entity_id)
    return await _ws_send(client, payload)


def _make_helper_handler(helper_type: str) -> DomainHandler:
    async def fetch(client: Any, entity_id: str) -> Any:
        return await _fetch_helper(client, entity_id, helper_type)

    async def restore(client: Any, entity_id: str, config: Any) -> Any:
        return await _restore_helper(client, entity_id, config, helper_type)

    return DomainHandler(domain=f"helper_{helper_type}", fetch=fetch, restore=restore)


# --------------------------- registry assembly ------------------------------

# Helper types the registry knows about. Add to this list to extend
# helper-domain coverage; the decorator's ``domain_fn`` builds the
# matching key at runtime.
_KNOWN_HELPER_TYPES = [
    "input_boolean",
    "input_text",
    "input_number",
    "input_select",
    "input_datetime",
    "input_button",
    "counter",
    "timer",
    "schedule",
    "template",
    "group",
    "utility_meter",
    "min_max",
    "threshold",
    "derivative",
    "integration",
    "statistics",
    "filter",
    "generic_thermostat",
    "trend",
    "history_stats",
    "tod",
    "combine_sensors",
    "random",
]


def register_default_handlers(mgr: BackupManager, _client: Any) -> None:
    mgr.register(DomainHandler("automation", _fetch_automation, _restore_automation))
    mgr.register(DomainHandler("script", _fetch_script, _restore_script))
    mgr.register(DomainHandler("scene", _fetch_scene, _restore_scene))
    mgr.register(DomainHandler("dashboard", _fetch_dashboard, _restore_dashboard))
    mgr.register(
        DomainHandler(
            "dashboard_resource", _fetch_dashboard_resource, _restore_dashboard_resource
        )
    )
    mgr.register(DomainHandler("label", _fetch_label, _restore_label))
    mgr.register(DomainHandler("category", _fetch_category, _restore_category))
    mgr.register(DomainHandler("group", _fetch_group, _restore_group))
    mgr.register(
        DomainHandler("calendar_event", _fetch_calendar_event, _restore_calendar_event)
    )
    mgr.register(DomainHandler("zone", _fetch_zone, _restore_zone))
    mgr.register(
        DomainHandler("area_or_floor", _fetch_area_or_floor, _restore_area_or_floor)
    )
    mgr.register(DomainHandler("todo_item", _fetch_todo_item, _restore_todo_item))
    mgr.register(DomainHandler("entity", _fetch_entity_state, _restore_entity_state))
    mgr.register(DomainHandler("integration", _fetch_integration, _restore_integration))
    for helper_type in _KNOWN_HELPER_TYPES:
        mgr.register(_make_helper_handler(helper_type))
