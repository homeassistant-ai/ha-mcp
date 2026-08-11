#!/usr/bin/env python3
"""Auditable, fixed-scope bootstrap for the Aurora deployment adapter.

Credentials are read only from the fixed approved main-repository ``.env``.
Recovery pins and lifecycle leases live under that repository's mode-0700 local
state directory. The remote installer
has no caller-selectable paths, commands, or URLs and can touch only the fixed
Aurora component, trust store, configuration key, lock, stage, and transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import urlencode, urljoin, urlparse

FILE_EDITOR_SLUG = "core_configurator"
FILE_EDITOR_VERSION = "6.1.0"
APPROVED_BACKUP_AGENT_ID = "hassio.local"
REMOTE_ROOT = "/homeassistant"
REMOTE_CONFIGURATION = f"{REMOTE_ROOT}/configuration.yaml"
REMOTE_COMPONENT = f"{REMOTE_ROOT}/custom_components/aurora_deploy"
REMOTE_TRUST = f"{REMOTE_ROOT}/aurora_deploy_trusted_keys.json"
REMOTE_LOCK = f"{REMOTE_ROOT}/.aurora-deploy-bootstrap-global.lock"
LOCK_CANDIDATE_PREFIX = ".aurora-deploy-bootstrap-lock-candidate-"
STAGE_PREFIX = ".aurora-deploy-bootstrap-stage-"
TRANSACTION_PREFIX = ".aurora-deploy-bootstrap-transaction-"
TRANSACTION_INIT_PREFIX = ".aurora-deploy-bootstrap-transaction-init-"
INSTALLER_NAME = "install-aurora-deploy-bootstrap.py"
PAYLOAD_MANIFEST_NAME = "payload-manifest.json"
CAPABILITY_PROBE_NAME = "fixed-root-capability-probe.json"
COMPONENT_FILES = ("__init__.py", "adapter.py", "manifest.json")
SOURCE_PATHS = tuple(
    f"custom_components/aurora_deploy/{name}" for name in COMPONENT_FILES
)
TRUST_PATH = "aurora_deploy_trusted_keys.json"
PAYLOAD_PATHS = (*SOURCE_PATHS, TRUST_PATH)
APPROVED_REPOSITORY_ROOT = Path(
    "/Users/terencevanrooyen/Development/Private/home-assistant"
)
APPROVED_CREDENTIAL_ENV = APPROVED_REPOSITORY_ROOT / ".env"
APPROVED_STATE_DIRECTORY = APPROVED_REPOSITORY_ROOT / "local/aurora-deploy-state"
ROLLBACK_WINDOW_SECONDS = 24 * 60 * 60
STALE_LOCK_SECONDS = 60 * 60
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BACKUP_AGE = timedelta(hours=24)
MAX_BACKUP_FUTURE_SKEW = timedelta(minutes=5)
SAFE_INGRESS_ENTRY = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]+$")
SAFE_SESSION = re.compile(r"^[A-Za-z0-9._~+/=-]{16,1024}$")
SAFE_BACKUP_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
SAFE_KEY_ID = re.compile(r"^(?=.{8,64}$)(?:release|validation)-[A-Za-z0-9._-]+$")
SAFE_HAOS_VERSION = re.compile(r"^[0-9][A-Za-z0-9._+-]{0,31}$")
SAFE_OS_AGENT_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SAFE_PYCACHE = re.compile(
    r"^__pycache__/(?:__init__|adapter)\.cpython-\d{3}(?:\.opt-[12])?\.pyc$"
)
REQUIRED_HAOS_FEATURES = frozenset({"haos", "os_agent"})
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40,64}$")


class BootstrapError(RuntimeError):
    """Stable, non-sensitive bootstrap failure code."""


class PayloadFile(NamedTuple):
    relative_path: str
    content: bytes
    sha256: str
    size: int


class Payload(NamedTuple):
    files: tuple[PayloadFile, ...]
    manifest: bytes
    manifest_sha256: str
    release_key_sha256: str
    validation_key_sha256: str
    source_revision: str


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError("json_duplicate_key")
        result[key] = value
    return result


def _json_object(content: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode(), object_pairs_hook=_reject_duplicate_pairs)
    except BootstrapError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BootstrapError(code) from None
    if not isinstance(value, dict):
        raise BootstrapError(code)
    return value


def _validate_uuid(value: str | None) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise BootstrapError("transaction_id_required") from None
    if parsed.version != 4 or str(parsed) != value:
        raise BootstrapError("transaction_id_invalid")
    return value


def _validate_hash(value: str | None, code: str, *, absent: bool = False) -> str:
    if absent and value == "absent":
        return value
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise BootstrapError(code)
    return value


def _validate_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BootstrapError("home_assistant_url_invalid")
    if parsed.scheme == "http":
        host = parsed.hostname.rstrip(".").lower()
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise BootstrapError("https_required")
    return value.rstrip("/")


def _load_credentials() -> tuple[str, str]:  # noqa: C901
    """Load credentials only from the fixed main-repository ``.env``."""
    env_path = APPROVED_CREDENTIAL_ENV
    try:
        info = env_path.lstat()
    except OSError:
        raise BootstrapError("root_env_required") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BootstrapError("root_env_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BootstrapError("root_env_permissions_unsafe")
    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise BootstrapError("root_env_invalid") from None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"}:
            continue
        if key in values:
            raise BootstrapError("root_env_duplicate_credential")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not value or "\r" in value or "\n" in value or "${" in value:
            raise BootstrapError("root_env_invalid")
        values[key] = value
    if set(values) != {"HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"}:
        raise BootstrapError("root_env_credentials_required")
    return _validate_url(values["HOMEASSISTANT_URL"]), values["HOMEASSISTANT_TOKEN"]


def _decode_public_key(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > 128:
        raise BootstrapError("trust_store_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise BootstrapError("trust_store_invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode() != value:
        raise BootstrapError("trust_store_invalid")
    return decoded


def validate_trust_store(content: bytes) -> tuple[str, str]:
    value = _json_object(content, "trust_store_invalid")
    if any(not isinstance(key, str) for key in value):
        raise BootstrapError("trust_store_invalid")
    release = [key for key in value if key.startswith("release-")]
    validation = [key for key in value if key.startswith("validation-")]
    if len(release) != 1 or len(validation) != 1:
        raise BootstrapError("trust_store_roles_invalid")
    if len(value) != 2 or any(SAFE_KEY_ID.fullmatch(key) is None for key in value):
        raise BootstrapError("trust_store_invalid")
    release_raw = _decode_public_key(value[release[0]])
    validation_raw = _decode_public_key(value[validation[0]])
    if secrets.compare_digest(release_raw, validation_raw):
        raise BootstrapError("trust_store_keys_not_distinct")
    return _digest(release_raw), _digest(validation_raw)


def _regular_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    try:
        info = path.lstat()
    except OSError:
        raise BootstrapError("source_file_unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BootstrapError("source_file_invalid")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError:
        raise BootstrapError("source_file_invalid") from None
    if resolved != resolved_root / relative or info.st_size > MAX_FILE_BYTES:
        raise BootstrapError("source_file_invalid")
    return path.read_bytes()


def _git_output(source_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError("source_revision_unavailable") from None
    if result.returncode != 0:
        raise BootstrapError("source_revision_unavailable")
    return result.stdout.strip()


def validate_local_payload(  # noqa: C901 - exact source and independent pin gate
    source_root: Path,
    *,
    expected_manifest_sha256: str,
    expected_release_key_sha256: str,
    expected_validation_key_sha256: str,
    expected_source_revision: str,
) -> Payload:
    """Validate exact committed source and independently supplied pins."""
    expected_manifest_sha256 = _validate_hash(
        expected_manifest_sha256, "expected_payload_manifest_hash_required"
    )
    expected_release_key_sha256 = _validate_hash(
        expected_release_key_sha256, "expected_release_key_fingerprint_required"
    )
    expected_validation_key_sha256 = _validate_hash(
        expected_validation_key_sha256, "expected_validation_key_fingerprint_required"
    )
    if REVISION.fullmatch(expected_source_revision) is None:
        raise BootstrapError("expected_source_revision_required")
    try:
        root_info = source_root.lstat()
        root = source_root.resolve(strict=True)
    except OSError:
        raise BootstrapError("source_root_unavailable") from None
    if stat.S_ISLNK(root_info.st_mode) or not root.is_dir():
        raise BootstrapError("source_root_invalid")
    component_directory = root / "custom_components" / "aurora_deploy"
    try:
        component_info = component_directory.lstat()
    except OSError:
        raise BootstrapError("source_component_unavailable") from None
    if stat.S_ISLNK(component_info.st_mode) or not stat.S_ISDIR(component_info.st_mode):
        raise BootstrapError("source_component_invalid")
    actual_component_files: set[str] = set()
    for item in component_directory.iterdir():
        if item.name == "__pycache__" and item.is_dir() and not item.is_symlink():
            continue
        if item.is_symlink() or not item.is_file():
            raise BootstrapError("source_component_file_set_invalid")
        actual_component_files.add(item.name)
    if actual_component_files != set(COMPONENT_FILES):
        raise BootstrapError("source_component_file_set_invalid")
    revision = _git_output(root, "rev-parse", "HEAD")
    if revision != expected_source_revision:
        raise BootstrapError("source_revision_mismatch")
    if _git_output(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise BootstrapError("source_worktree_not_clean")
    for relative in PAYLOAD_PATHS:
        _git_output(root, "ls-files", "--error-unmatch", "--", relative)
    files = tuple(
        PayloadFile(relative, content, _digest(content), len(content))
        for relative in PAYLOAD_PATHS
        for content in (_regular_bytes(root, relative),)
    )
    component_manifest = next(
        item for item in files if item.relative_path.endswith("/manifest.json")
    )
    _json_object(component_manifest.content, "component_manifest_invalid")
    trust = next(item for item in files if item.relative_path == TRUST_PATH)
    release_digest, validation_digest = validate_trust_store(trust.content)
    if release_digest != expected_release_key_sha256:
        raise BootstrapError("release_key_fingerprint_mismatch")
    if validation_digest != expected_validation_key_sha256:
        raise BootstrapError("validation_key_fingerprint_mismatch")
    manifest = _canonical(
        {
            "schemaVersion": "aurora-deploy-bootstrap-v2",
            "component": "aurora_deploy",
            "configurationKey": "aurora_deploy",
            "sourceRevision": revision,
            "files": [
                {"path": item.relative_path, "sha256": item.sha256, "size": item.size}
                for item in files
            ],
            "trust": {
                "releaseKeySha256": release_digest,
                "validationKeySha256": validation_digest,
            },
        }
    )
    manifest_digest = _digest(manifest)
    if manifest_digest != expected_manifest_sha256:
        raise BootstrapError("payload_manifest_hash_mismatch")
    return Payload(
        files,
        manifest,
        manifest_digest,
        release_digest,
        validation_digest,
        revision,
    )


def _parse_backup_date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise BootstrapError("protected_ha_recovery_backup_required")
    value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise BootstrapError("protected_ha_recovery_backup_required") from None
    if parsed.tzinfo is None:
        raise BootstrapError("protected_ha_recovery_backup_required")
    return parsed.astimezone(UTC)


def _fresh_ha_recovery_backup(
    backups: Any,
    backup_id: str,
    expected_agent_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(backups, list):
        return None
    matches = [
        item
        for item in backups
        if isinstance(item, dict) and item.get("backup_id") == backup_id
    ]
    if len(matches) != 1:
        return None
    item = matches[0]
    agents = item.get("agents")
    if not isinstance(agents, dict) or len(agents) != 1:
        return None
    agent_id, agent = next(iter(agents.items()))
    if agent_id != expected_agent_id or not isinstance(agent, dict):
        return None
    size = agent.get("size")
    healthy_agent = (
        agent.get("protected") is True
        and isinstance(size, (int, float))
        and not isinstance(size, bool)
        and math.isfinite(size)
        and size > 0
        and agent.get("status") in {None, "available"}
    )
    no_agent_failures = item.get("failed_agent_ids") in (None, []) and item.get(
        "agent_errors"
    ) in (None, {})
    if not (
        healthy_agent
        and no_agent_failures
        and isinstance(item.get("name"), str)
        and bool(item["name"].strip())
        and item.get("database_included") is True
        and item.get("homeassistant_included") is True
    ):
        return None
    age = (now or datetime.now(UTC)) - _parse_backup_date(item.get("date"))
    if age < -MAX_BACKUP_FUTURE_SKEW or age > MAX_BACKUP_AGE:
        return None
    return item


def validate_supervisor_preflight(  # noqa: C901
    *,
    user: dict[str, Any],
    supervisor: dict[str, Any],
    host: dict[str, Any],
    backup: dict[str, Any] | None,
    addon: dict[str, Any],
    store: dict[str, Any],
    backup_id: str | None,
    expected_backup_agent_id: str | None,
    require_backup: bool,
    now: datetime | None = None,
) -> str:
    if user.get("is_admin") is not True:
        raise BootstrapError("administrator_required")
    if (
        supervisor.get("healthy") is not True
        or supervisor.get("supported") is not True
        or ("state" in supervisor and supervisor["state"] != "running")
    ):
        raise BootstrapError("supervisor_not_ready")
    if supervisor.get("arch") != "amd64":
        raise BootstrapError("unsupported_architecture")
    host_arch_fields = [key for key in ("arch", "architecture") if key in host]
    if host_arch_fields:
        if any(host[key] != "amd64" for key in host_arch_fields):
            raise BootstrapError("unsupported_architecture")
    else:
        operating_system = host.get("operating_system")
        agent_version = host.get("agent_version")
        features = host.get("features")
        if (
            host.get("deployment") != "production"
            or not isinstance(operating_system, str)
            or not operating_system.startswith("Home Assistant OS ")
            or SAFE_HAOS_VERSION.fullmatch(
                operating_system.removeprefix("Home Assistant OS ")
            )
            is None
            or not isinstance(agent_version, str)
            or SAFE_OS_AGENT_VERSION.fullmatch(agent_version) is None
            or not isinstance(features, list)
            or len(features) > 64
            or any(
                not isinstance(feature, str) or not feature or len(feature) > 64
                for feature in features
            )
            or not REQUIRED_HAOS_FEATURES.issubset(features)
        ):
            raise BootstrapError("host_metadata_invalid")
    if host.get("features") is not None and not isinstance(host["features"], list):
        raise BootstrapError("host_metadata_invalid")
    if require_backup:
        if (
            expected_backup_agent_id != APPROVED_BACKUP_AGENT_ID
            or not isinstance(backup_id, str)
            or SAFE_BACKUP_ID.fullmatch(backup_id) is None
            or not isinstance(backup, dict)
            or backup.get("state") not in {None, "idle"}
            or backup.get("failed_agent_ids") not in (None, [])
            or backup.get("agent_errors") not in (None, {})
            or _fresh_ha_recovery_backup(
                backup.get("backups"),
                backup_id,
                expected_backup_agent_id,
                now=now,
            )
            is None
        ):
            raise BootstrapError("protected_ha_recovery_backup_required")
    if addon.get("slug") != FILE_EDITOR_SLUG or addon.get("name") != "File editor":
        raise BootstrapError("file_editor_identity_mismatch")
    if addon.get("version") != FILE_EDITOR_VERSION:
        raise BootstrapError("file_editor_version_mismatch")
    if addon.get("state") not in {"started", "stopped"}:
        raise BootstrapError("file_editor_state_unsafe")
    if addon.get("protected") is not True or addon.get("ingress") is not True:
        raise BootstrapError("file_editor_boundary_mismatch")
    ingress = addon.get("ingress_entry")
    if not isinstance(ingress, str) or SAFE_INGRESS_ENTRY.fullmatch(ingress) is None:
        raise BootstrapError("ingress_entry_invalid")
    if addon.get("host_network") is not False or (
        "network" in addon and addon["network"] is not None
    ):
        raise BootstrapError("file_editor_network_exposed")
    if addon.get("repository") != "core":
        raise BootstrapError("file_editor_not_official")
    store_version = store.get("version_latest") or store.get("version")
    if (
        store.get("slug") != FILE_EDITOR_SLUG
        or store.get("name") != "File editor"
        or store_version != FILE_EDITOR_VERSION
        or store.get("repository") != "core"
        or store.get("ingress") is not True
        or not isinstance(store.get("arch"), list)
        or "amd64" not in store["arch"]
        or store.get("available") is not True
    ):
        raise BootstrapError("file_editor_store_mismatch")
    if "network" in store and store["network"] is not None:
        raise BootstrapError("file_editor_network_exposed")
    return ingress


class HomeAssistantSocket:
    def __init__(self, url: str, token: str) -> None:
        self._url, self._token, self._socket, self._id = url, token, None, 0

    async def __aenter__(self) -> HomeAssistantSocket:
        import websockets

        parsed = urlparse(self._url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        try:
            self._socket = await websockets.connect(
                f"{scheme}://{parsed.netloc}/api/websocket",
                open_timeout=20,
                max_size=MAX_RESPONSE_BYTES,
            )
            if json.loads(await self._socket.recv()).get("type") != "auth_required":
                raise BootstrapError("websocket_auth_failed")
            await self._socket.send(
                json.dumps({"type": "auth", "access_token": self._token})
            )
            if json.loads(await self._socket.recv()).get("type") != "auth_ok":
                raise BootstrapError("websocket_auth_failed")
        except BootstrapError:
            raise
        except Exception:
            raise BootstrapError("websocket_connection_failed") from None
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self._socket is not None:
            await self._socket.close()

    async def call(self, command: str, **fields: Any) -> dict[str, Any]:
        self._id += 1
        try:
            await self._socket.send(
                json.dumps({"id": self._id, "type": command, **fields})
            )
            response = json.loads(await asyncio.wait_for(self._socket.recv(), 180))
        except Exception:
            raise BootstrapError("websocket_command_failed") from None
        if (
            not isinstance(response, dict)
            or response.get("id") != self._id
            or response.get("type") != "result"
            or response.get("success") is not True
            or not isinstance(response.get("result"), dict)
        ):
            raise BootstrapError("websocket_command_failed")
        return response["result"]


def _supervisor_data(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("result") == "ok" and isinstance(value.get("data"), dict):
        return value["data"]
    if "result" not in value:
        return value
    raise BootstrapError("supervisor_response_invalid")


def _validated_installer_receipt(
    value: dict[str, Any], mode: str, transaction_id: str
) -> dict[str, Any]:
    allowed = {
        "status",
        "transactionId",
        "payloadManifestSha256",
        "configurationSha256",
        "componentTreeSha256",
        "trustSha256",
        "rollbackDeadline",
        "installerSha256",
    }
    if set(value) - allowed or value.get("transactionId") != transaction_id:
        raise BootstrapError("installer_output_invalid")
    expected = {
        "install": "installed",
        "rollback": "rolled_back",
        "recover-lock": "lock_recovered",
        "abort-stage": "stage_aborted",
        "mark-verified": "restart_verified",
        "finalize": "finalized",
    }[mode]
    if value.get("status") != expected:
        raise BootstrapError("installer_output_invalid")
    for key in (
        "payloadManifestSha256",
        "configurationSha256",
        "componentTreeSha256",
    ):
        if key in value and SHA256.fullmatch(str(value[key])) is None:
            raise BootstrapError("installer_output_invalid")
    if (
        "trustSha256" in value
        and value["trustSha256"] != "absent"
        and SHA256.fullmatch(str(value["trustSha256"])) is None
    ):
        raise BootstrapError("installer_output_invalid")
    if "rollbackDeadline" in value and (
        not isinstance(value["rollbackDeadline"], int)
        or isinstance(value["rollbackDeadline"], bool)
    ):
        raise BootstrapError("installer_output_invalid")
    return value


TRANSACTION_STATUSES = {
    "prepared",
    "installed",
    "rollback_prepared",
    "rolled_back",
    "rolled_back_after_failure",
    "rolled_back_after_recovery",
    "restart_verified",
    "finalize_prepared",
    "rollback_finalize_prepared",
}


def _validated_file_state(value: Any, *, required: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise BootstrapError("transaction_invalid")
    if value["exists"] is False:
        if required or set(value) != {"exists"}:
            raise BootstrapError("transaction_invalid")
        return value
    if (
        set(value) != {"exists", "sha256", "size", "mode", "uid", "gid"}
        or SHA256.fullmatch(str(value.get("sha256", ""))) is None
        or any(
            not isinstance(value.get(key), int) or isinstance(value.get(key), bool)
            for key in ("size", "mode", "uid", "gid")
        )
        or value["size"] < 0
    ):
        raise BootstrapError("transaction_invalid")
    return value


def _component_state_digest(value: Any) -> str:  # noqa: C901
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise BootstrapError("transaction_invalid")
    if value["exists"] is False:
        if value != {"exists": False, "files": {}, "directories": {}}:
            raise BootstrapError("transaction_invalid")
        return _digest(_canonical({"exists": False, "files": {}, "directories": []}))
    if (
        set(value) != {"exists", "files", "directories"}
        or not isinstance(value["files"], dict)
        or not isinstance(value["directories"], dict)
        or len(value["files"]) + len(value["directories"]) > 129
    ):
        raise BootstrapError("transaction_invalid")
    files: dict[str, str] = {}
    for name, state in value["files"].items():
        if not isinstance(name, str):
            raise BootstrapError("transaction_invalid")
        _validated_file_state({"exists": True, **state}, required=True)
        if name.startswith("__pycache__/"):
            if SAFE_PYCACHE.fullmatch(name) is None:
                raise BootstrapError("component_generated_artifact_invalid")
            continue
        files[name] = state["sha256"]
    directories: list[str] = []
    for name, state in value["directories"].items():
        if (
            not isinstance(name, str)
            or not isinstance(state, dict)
            or set(state) != {"mode", "uid", "gid"}
            or any(
                not isinstance(state.get(key), int) or isinstance(state.get(key), bool)
                for key in ("mode", "uid", "gid")
            )
        ):
            raise BootstrapError("transaction_invalid")
        if name in {".", "__pycache__"}:
            continue
        if name.startswith("__pycache__/"):
            raise BootstrapError("component_generated_artifact_invalid")
        directories.append(name)
    if "." not in value["directories"]:
        raise BootstrapError("transaction_invalid")
    return _digest(
        _canonical({"exists": True, "files": files, "directories": sorted(directories)})
    )


def _validated_transaction_journal(
    value: dict[str, Any], transaction_id: str
) -> dict[str, Any]:
    allowed = {
        "schemaVersion",
        "transactionId",
        "payloadManifestSha256",
        "installerSha256",
        "replaceExact",
        "status",
        "createdAt",
        "rollbackDeadline",
        "prestate",
        "installed",
        "configurationSha256",
        "componentTreeSha256",
        "trustSha256",
    }
    if (
        set(value) - allowed
        or value.get("schemaVersion") != "aurora-deploy-bootstrap-transaction-v2"
        or value.get("transactionId") != transaction_id
        or value.get("status") not in TRANSACTION_STATUSES
        or SHA256.fullmatch(str(value.get("payloadManifestSha256", ""))) is None
        or SHA256.fullmatch(str(value.get("installerSha256", ""))) is None
        or not isinstance(value.get("replaceExact"), bool)
    ):
        raise BootstrapError("transaction_invalid")
    created = value.get("createdAt")
    deadline = value.get("rollbackDeadline")
    if (
        not isinstance(created, int)
        or isinstance(created, bool)
        or not isinstance(deadline, int)
        or isinstance(deadline, bool)
        or deadline != created + ROLLBACK_WINDOW_SECONDS
    ):
        raise BootstrapError("transaction_invalid")
    for state_name in ("prestate", "installed"):
        state = value.get(state_name)
        if (
            not isinstance(state, dict)
            or set(state) != {"configuration", "component", "trust"}
            or any(not isinstance(state.get(key), dict) for key in state)
            or any(
                not isinstance(state[key].get("exists"), bool)
                for key in ("configuration", "component", "trust")
            )
        ):
            raise BootstrapError("transaction_invalid")
        _validated_file_state(state["configuration"], required=True)
        _validated_file_state(state["trust"], required=state_name == "installed")
        _component_state_digest(state["component"])
        if state_name == "installed" and state["component"].get("exists") is not True:
            raise BootstrapError("transaction_invalid")
    summary_keys = {
        "configurationSha256",
        "componentTreeSha256",
        "trustSha256",
    }
    if value["status"] == "prepared":
        if summary_keys & set(value):
            raise BootstrapError("transaction_invalid")
    else:
        source = (
            value["installed"]
            if value["status"]
            in {
                "installed",
                "restart_verified",
                "finalize_prepared",
                "rollback_prepared",
            }
            else value["prestate"]
        )
        expected_summary = {
            "configurationSha256": source["configuration"]["sha256"],
            "componentTreeSha256": _component_state_digest(source["component"]),
            "trustSha256": source["trust"].get("sha256", "absent"),
        }
        if any(
            value.get(key) != expected for key, expected in expected_summary.items()
        ):
            raise BootstrapError("transaction_invalid")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class FileEditorIngressClient:
    """Fixed File editor surface; no public method accepts a remote path."""

    def __init__(
        self, url: str, entry: str, session: str, *, opener: Any | None = None
    ) -> None:
        self._url = _validate_url(url)
        if (
            SAFE_INGRESS_ENTRY.fullmatch(entry) is None
            or SAFE_SESSION.fullmatch(session) is None
        ):
            raise BootstrapError("ingress_session_invalid")
        self._entry, self._session = entry, session
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _url_for(self, endpoint: str, query: dict[str, str] | None = None) -> str:
        if endpoint not in {
            "/api/listdir",
            "/api/newfolder",
            "/api/upload",
            "/api/delete",
            "/api/file",
            "/api/exec_command",
        }:
            raise BootstrapError("file_editor_endpoint_invalid")
        base = urljoin(f"{self._url}/", f"{self._entry.lstrip('/').rstrip('/')}/")
        target = urljoin(base, endpoint.lstrip("/"))
        return f"{target}?{urlencode(query)}" if query else target

    def _open(self, request: urllib.request.Request, timeout: float = 30) -> bytes:
        request.add_header("Cookie", f"ingress_session={self._session}")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise BootstrapError("file_editor_http_failure")
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except BootstrapError:
            raise
        except Exception:
            raise BootstrapError("file_editor_transport_failure") from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise BootstrapError("file_editor_response_too_large")
        return content

    def _request(
        self,
        endpoint: str,
        *,
        query: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        data = urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(
            self._url_for(endpoint, query),
            data=data,
            method="POST" if data is not None else "GET",
        )
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return _json_object(
            self._open(request, timeout), "file_editor_response_invalid"
        )

    @staticmethod
    def _stage(transaction_id: str, relative: str = "") -> str:
        transaction_id = _validate_uuid(transaction_id)
        root = f"{REMOTE_ROOT}/{STAGE_PREFIX}{transaction_id}"
        return f"{root}/{relative}" if relative else root

    @staticmethod
    def _transaction(transaction_id: str, relative: str = "") -> str:
        transaction_id = _validate_uuid(transaction_id)
        root = f"{REMOTE_ROOT}/{TRANSACTION_PREFIX}{transaction_id}"
        return f"{root}/{relative}" if relative else root

    @staticmethod
    def _initialization(transaction_id: str) -> str:
        transaction_id = _validate_uuid(transaction_id)
        return f"{REMOTE_ROOT}/{TRANSACTION_INIT_PREFIX}{transaction_id}"

    def _list(self, path: str) -> list[dict[str, Any]]:
        result = self._request("/api/listdir", query={"path": path})
        if (
            result.get("error") not in {None, False}
            or result.get("abspath") != path
            or not isinstance(result.get("content"), list)
        ):
            raise BootstrapError("remote_path_mismatch")
        return result["content"]

    def _exists(self, fixed_path: str, *, expected_type: str) -> bool:
        parent, name = (
            str(PurePosixPath(fixed_path).parent),
            PurePosixPath(fixed_path).name,
        )
        matches = [
            item
            for item in self._list(parent)
            if isinstance(item, dict) and item.get("name") == name
        ]
        if not matches:
            return False
        if (
            len(matches) != 1
            or matches[0].get("type") != expected_type
            or matches[0].get("fullpath") != fixed_path
        ):
            raise BootstrapError("remote_path_invalid")
        return True

    def transaction_exists(self, transaction_id: str) -> bool:
        return self._exists(self._transaction(transaction_id), expected_type="dir")

    def initialization_exists(self, transaction_id: str) -> bool:
        return self._exists(self._initialization(transaction_id), expected_type="dir")

    def stage_exists(self, transaction_id: str) -> bool:
        return self._exists(self._stage(transaction_id), expected_type="dir")

    def stage_file_exists(self, transaction_id: str, relative: str) -> bool:
        if relative not in {
            *PAYLOAD_PATHS,
            PAYLOAD_MANIFEST_NAME,
            INSTALLER_NAME,
            CAPABILITY_PROBE_NAME,
        }:
            raise BootstrapError("stage_target_invalid")
        return self._exists(self._stage(transaction_id, relative), expected_type="file")

    def _mkdir(self, path: str) -> None:
        parent, name = str(PurePosixPath(path).parent), PurePosixPath(path).name
        result = self._request("/api/newfolder", form={"path": parent, "name": name})
        if result.get("error") is not False or result.get("path") != path:
            raise BootstrapError("remote_directory_create_failed")

    def create_stage(self, transaction_id: str) -> None:
        root = self._stage(transaction_id)
        for directory in (
            root,
            f"{root}/custom_components",
            f"{root}/custom_components/aurora_deploy",
        ):
            if not self._exists(directory, expected_type="dir"):
                self._mkdir(directory)

    def upload(self, transaction_id: str, relative: str, content: bytes) -> None:
        if relative not in {
            *PAYLOAD_PATHS,
            PAYLOAD_MANIFEST_NAME,
            INSTALLER_NAME,
            CAPABILITY_PROBE_NAME,
        }:
            raise BootstrapError("upload_target_invalid")
        target = self._stage(transaction_id, relative)
        destination, filename = (
            str(PurePosixPath(target).parent),
            PurePosixPath(target).name,
        )
        boundary = f"aurora-{secrets.token_hex(16)}"
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n{destination}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        result = _json_object(
            self._open(
                urllib.request.Request(
                    self._url_for("/api/upload"),
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}"
                    },
                )
            ),
            "file_editor_response_invalid",
        )
        # hass-configurator 0.6.0 does not echo the destination. Treat only
        # its complete success object as acknowledgement; the fixed-target
        # list/download/hash readback in _stage_payload remains authoritative.
        if (
            set(result) != {"error", "message"}
            or result["error"] is not False
            or result["message"] != "Upload successful"
        ):
            raise BootstrapError("remote_upload_failed")

    def _download_path(self, path: str) -> bytes:
        return self._open(
            urllib.request.Request(self._url_for("/api/file", {"filename": path}))
        )

    def download_stage(self, transaction_id: str, relative: str) -> bytes:
        if relative not in {
            *PAYLOAD_PATHS,
            PAYLOAD_MANIFEST_NAME,
            INSTALLER_NAME,
            CAPABILITY_PROBE_NAME,
        }:
            raise BootstrapError("download_target_invalid")
        return self._download_path(self._stage(transaction_id, relative))

    def _delete_stage_capability_probe(self, transaction_id: str) -> None:
        target = self._stage(transaction_id, CAPABILITY_PROBE_NAME)
        result = self._request("/api/delete", form={"path": target})
        if (
            set(result) != {"error", "message", "path"}
            or result["error"] is not False
            or result["message"] != "Deletion successful"
            or result["path"] != target
        ):
            raise BootstrapError("fixed_root_capability_cleanup_failed")
        if self.stage_file_exists(transaction_id, CAPABILITY_PROBE_NAME):
            raise BootstrapError("fixed_root_capability_cleanup_failed")

    def verify_fixed_root_write_capability(self, transaction_id: str) -> None:
        marker = _canonical(
            {
                "schemaVersion": "aurora-deploy-fixed-root-capability-v1",
                "transactionId": _validate_uuid(transaction_id),
                "installerSha256": INSTALLER_SHA256,
            }
        )
        expected = _digest(marker)
        if self.stage_file_exists(transaction_id, CAPABILITY_PROBE_NAME):
            if (
                _digest(self.download_stage(transaction_id, CAPABILITY_PROBE_NAME))
                != expected
            ):
                raise BootstrapError("fixed_root_capability_conflict")
            self._delete_stage_capability_probe(transaction_id)
        self.upload(transaction_id, CAPABILITY_PROBE_NAME, marker)
        if (
            not self.stage_file_exists(transaction_id, CAPABILITY_PROBE_NAME)
            or _digest(self.download_stage(transaction_id, CAPABILITY_PROBE_NAME))
            != expected
        ):
            raise BootstrapError("fixed_root_write_capability_required")
        self._delete_stage_capability_probe(transaction_id)

    def configuration_bytes(self) -> bytes:
        if not self._exists(REMOTE_CONFIGURATION, expected_type="file"):
            raise BootstrapError("remote_configuration_missing")
        return self._download_path(REMOTE_CONFIGURATION)

    def trust_sha256(self) -> str:
        if not self._exists(REMOTE_TRUST, expected_type="file"):
            return "absent"
        return _digest(self._download_path(REMOTE_TRUST))

    def component_tree_sha256(self) -> str:  # noqa: C901
        if not self._exists(REMOTE_COMPONENT, expected_type="dir"):
            return _digest(
                _canonical({"exists": False, "files": {}, "directories": []})
            )
        files: dict[str, str] = {}
        directories: set[str] = set()
        pending = [(REMOTE_COMPONENT, "")]
        total_bytes = 0
        while pending:
            directory, prefix = pending.pop()
            for item in self._list(directory):
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise BootstrapError("remote_component_invalid")
                name = item["name"]
                if name in {".", ".."} or "/" in name or "\\" in name:
                    raise BootstrapError("remote_component_invalid")
                relative = f"{prefix}/{name}".lstrip("/")
                if (
                    len(PurePosixPath(relative).parts) > 8
                    or len(files) + len(pending) >= 128
                ):
                    raise BootstrapError("remote_component_too_large")
                expected = f"{directory}/{name}"
                if item.get("fullpath") != expected or item.get("type") not in {
                    "file",
                    "dir",
                }:
                    raise BootstrapError("remote_component_invalid")
                if item["type"] == "dir":
                    if relative == "__pycache__":
                        pass
                    elif relative.startswith("__pycache__/"):
                        raise BootstrapError("component_generated_artifact_invalid")
                    else:
                        directories.add(relative)
                    pending.append((expected, relative))
                else:
                    content = self._download_path(expected)
                    total_bytes += len(content)
                    if total_bytes > 8 * 1024 * 1024:
                        raise BootstrapError("remote_component_too_large")
                    if SAFE_PYCACHE.fullmatch(relative) is not None:
                        continue
                    if relative.startswith("__pycache__/"):
                        raise BootstrapError("component_generated_artifact_invalid")
                    files[relative] = _digest(content)
        return _digest(
            _canonical(
                {
                    "exists": True,
                    "files": files,
                    "directories": sorted(directories),
                }
            )
        )

    def transaction_journal(self, transaction_id: str) -> dict[str, Any] | None:
        if not self.transaction_exists(transaction_id):
            return None
        return _validated_transaction_journal(
            _json_object(
                self._download_path(
                    self._transaction(transaction_id, "transaction.json")
                ),
                "transaction_invalid",
            ),
            transaction_id,
        )

    def lock_status(self) -> dict[str, Any] | None:
        if not self._exists(REMOTE_LOCK, expected_type="file"):
            return None
        lock = _json_object(self._download_path(REMOTE_LOCK), "global_lock_invalid")
        if (
            lock.get("schemaVersion") != "aurora-deploy-bootstrap-lock-v4"
            or not isinstance(lock.get("transactionId"), str)
            or not isinstance(lock.get("acquiredAt"), int)
            or SHA256.fullmatch(str(lock.get("installerSha256", ""))) is None
            or not isinstance(lock.get("processId"), int)
            or isinstance(lock.get("processId"), bool)
            or lock.get("processId") <= 0
            or not isinstance(lock.get("processStartTicks"), int)
            or isinstance(lock.get("processStartTicks"), bool)
            or lock.get("processStartTicks") < 0
            or not isinstance(lock.get("bootId"), str)
            or not isinstance(lock.get("probeNonce"), str)
            or re.fullmatch(r"[0-9a-f]{32}", lock["probeNonce"]) is None
        ):
            raise BootstrapError("global_lock_invalid")
        return lock

    def lock_candidate_exists(self, transaction_id: str) -> bool:
        transaction_id = _validate_uuid(transaction_id)
        return self._exists(
            f"{REMOTE_ROOT}/{LOCK_CANDIDATE_PREFIX}{transaction_id}",
            expected_type="file",
        )

    def status(self, transaction_id: str) -> dict[str, Any]:
        transaction_present = self.transaction_exists(transaction_id)
        journal = (
            self.transaction_journal(transaction_id) if transaction_present else None
        )
        lock = self.lock_status()
        stage_present = self.stage_exists(transaction_id)
        lock_candidate_present = self.lock_candidate_exists(transaction_id)
        initialization_present = self.initialization_exists(transaction_id)
        result: dict[str, Any] = {
            "status": "not_found",
            "transactionId": transaction_id,
            "stagePresent": stage_present,
            "lockCandidatePresent": lock_candidate_present,
            "transactionPresent": transaction_present,
            "initializationPresent": initialization_present,
        }
        if journal is not None:
            for source, target in (
                ("status", "status"),
                ("payloadManifestSha256", "payloadManifestSha256"),
                ("configurationSha256", "configurationSha256"),
                ("componentTreeSha256", "componentTreeSha256"),
                ("trustSha256", "trustSha256"),
                ("rollbackDeadline", "rollbackDeadline"),
                ("installerSha256", "installerSha256"),
                ("replaceExact", "replaceExact"),
            ):
                value = journal.get(source)
                if value is not None:
                    result[target] = value
            prestate = journal["prestate"]
            result.update(
                {
                    "prestateConfigurationSha256": prestate["configuration"].get(
                        "sha256"
                    ),
                    "prestateComponentTreeSha256": _component_state_digest(
                        prestate["component"]
                    ),
                    "prestateTrustSha256": prestate["trust"].get("sha256", "absent"),
                }
            )
        elif initialization_present:
            result["status"] = "initializing"
        elif lock_candidate_present:
            result["status"] = "lock_candidate"
        elif stage_present:
            result["status"] = "staged_partial"
        result["lockHeld"] = lock is not None
        result["lockOwnerMatches"] = (
            lock is not None and lock.get("transactionId") == transaction_id
        )
        return result

    def readback(self, transaction_id: str) -> dict[str, Any]:
        result = self.status(transaction_id)
        if result.get("status") == "not_found":
            return {**result, "verified": False}
        current = {
            "configurationSha256": _digest(self.configuration_bytes()),
            "componentTreeSha256": self.component_tree_sha256(),
            "trustSha256": self.trust_sha256(),
        }
        verified = all(result.get(key) == value for key, value in current.items())
        return {**result, **current, "verified": verified}

    def execute(  # noqa: C901 - fixed command and lost-response state machine
        self, *, mode: str, transaction_id: str, arguments: dict[str, str | bool]
    ) -> dict[str, Any]:
        if mode not in {
            "install",
            "rollback",
            "recover-lock",
            "abort-stage",
            "mark-verified",
            "finalize",
        }:
            raise BootstrapError("installer_mode_invalid")
        if mode in {"install", "abort-stage"}:
            installer = self._stage(transaction_id, INSTALLER_NAME)
        else:
            installer = self._transaction(transaction_id, INSTALLER_NAME)
            if mode == "recover-lock" and not self.transaction_exists(transaction_id):
                installer = self._stage(transaction_id, INSTALLER_NAME)
        expected_installer_sha256 = _validate_hash(
            arguments.get("expected-installer-sha256"),
            "expected_installer_hash_required",
        )
        if _digest(self._download_path(installer)) != expected_installer_sha256:
            raise BootstrapError("remote_installer_hash_mismatch")
        command = f"python3 {installer} {mode} --transaction-id {transaction_id}"
        allowed = {
            "expected-configuration-sha256",
            "expected-component-tree-sha256",
            "expected-trust-sha256",
            "payload-manifest-sha256",
            "expected-installer-sha256",
            "replace-exact",
        }
        if set(arguments) - allowed:
            raise BootstrapError("installer_argument_invalid")
        for key in sorted(arguments):
            value = arguments[key]
            if value is True:
                command += f" --{key}"
            elif isinstance(value, str) and (
                SHA256.fullmatch(value) or value == "absent"
            ):
                command += f" --{key} {value}"
            else:
                raise BootstrapError("installer_argument_invalid")
        if any(char in command for char in ";&|`$<>(){}[]\\\r\n"):
            raise BootstrapError("installer_command_invalid")
        try:
            wrapper = self._request(
                "/api/exec_command",
                form={"command": command, "timeout": "25"},
                timeout=35,
            )
        except BootstrapError as error:
            if str(error) not in {
                "file_editor_transport_failure",
                "file_editor_http_failure",
            }:
                raise
            return self._reconcile_lost_response(mode, transaction_id, arguments)
        try:
            if (
                set(wrapper) != {"error", "message", "returncode", "stdout", "stderr"}
                or wrapper["error"] is not False
                or wrapper["message"] != f"Command executed: {command}"
                or not isinstance(wrapper["returncode"], int)
                or isinstance(wrapper["returncode"], bool)
                or wrapper["returncode"] != 0
            ):
                raise BootstrapError("installer_outcome_unknown")
            stdout = wrapper.get("stdout")
            if (
                not isinstance(stdout, str)
                or wrapper.get("stderr") != ""
                or len(stdout) > 4096
            ):
                raise BootstrapError("installer_outcome_unknown")
            return _validated_installer_receipt(
                _json_object(stdout.encode(), "installer_output_invalid"),
                mode,
                transaction_id,
            )
        except Exception:
            return self._reconcile_lost_response(mode, transaction_id, arguments)

    def _reconcile_lost_response(
        self,
        mode: str,
        transaction_id: str,
        arguments: dict[str, str | bool],
    ) -> dict[str, Any]:
        first = self.readback(transaction_id)
        second = self.readback(transaction_id)
        stable_keys = {
            "status",
            "transactionId",
            "payloadManifestSha256",
            "installerSha256",
            "replaceExact",
            "configurationSha256",
            "componentTreeSha256",
            "trustSha256",
            "rollbackDeadline",
            "prestateConfigurationSha256",
            "prestateComponentTreeSha256",
            "prestateTrustSha256",
            "stagePresent",
            "lockCandidatePresent",
            "transactionPresent",
            "initializationPresent",
            "lockOwnerMatches",
            "verified",
        }
        topology_keys = {
            "stagePresent",
            "lockCandidatePresent",
            "transactionPresent",
            "initializationPresent",
            "lockHeld",
            "lockOwnerMatches",
            "verified",
        }
        if (
            first.get("transactionId") != transaction_id
            or second.get("transactionId") != transaction_id
            or any(
                not isinstance(snapshot.get(key), bool)
                for snapshot in (first, second)
                for key in topology_keys
            )
            or first.get("lockHeld") is not False
            or second.get("lockHeld") is not False
            or first.get("lockOwnerMatches") is not False
            or second.get("lockOwnerMatches") is not False
            or {key: first.get(key) for key in stable_keys}
            != {key: second.get(key) for key in stable_keys}
        ):
            raise BootstrapError("installer_result_ambiguous")
        status = second
        transaction_present = status.get("transactionPresent") is True
        if transaction_present:
            bindings = {
                "installerSha256": arguments.get("expected-installer-sha256"),
                "payloadManifestSha256": arguments.get("payload-manifest-sha256"),
                "prestateConfigurationSha256": arguments.get(
                    "expected-configuration-sha256"
                ),
                "prestateComponentTreeSha256": arguments.get(
                    "expected-component-tree-sha256"
                ),
                "prestateTrustSha256": arguments.get("expected-trust-sha256"),
                "replaceExact": bool(arguments.get("replace-exact", False)),
            }
            if any(
                (not isinstance(expected, (str, bool)) or status.get(key) != expected)
                for key, expected in bindings.items()
            ):
                raise BootstrapError("installer_result_ambiguous")
        expected = {
            "install": "installed",
            "rollback": "rolled_back",
            "recover-lock": "lock_recovered",
            "abort-stage": "stage_aborted",
            "mark-verified": "restart_verified",
            "finalize": "not_found",
        }[mode]
        if (
            mode == "recover-lock"
            and status.get("lockCandidatePresent") is False
            and status.get("initializationPresent") is False
            and status.get("status")
            in {
                "not_found",
                "staged_partial",
                "installed",
                "rolled_back",
                "rolled_back_after_failure",
                "rolled_back_after_recovery",
                "restart_verified",
                "finalize_prepared",
                "rollback_finalize_prepared",
            }
            and (
                (
                    status.get("status") == "not_found"
                    and status.get("stagePresent") is False
                    and transaction_present is False
                )
                or (
                    status.get("status") == "staged_partial"
                    and status.get("stagePresent") is True
                    and transaction_present is False
                )
                or (transaction_present and status.get("verified") is True)
            )
        ):
            return {
                "status": "lock_recovered",
                "transactionId": transaction_id,
                "reconciled": True,
            }
        if (
            mode == "abort-stage"
            and status.get("lockCandidatePresent") is False
            and status.get("initializationPresent") is False
            and status.get("transactionPresent") is False
            and status.get("stagePresent") is False
        ):
            return {
                "status": "stage_aborted",
                "transactionId": transaction_id,
                "reconciled": True,
            }
        if (
            mode == "finalize"
            and status.get("status") == "not_found"
            and status.get("lockCandidatePresent") is False
            and status.get("initializationPresent") is False
            and status.get("transactionPresent") is False
            and status.get("stagePresent") is False
        ):
            return {
                "status": "finalized",
                "transactionId": transaction_id,
                "reconciled": True,
            }
        if (
            status.get("status") != expected
            or status.get("verified") is not True
            or transaction_present is not True
            or status.get("lockCandidatePresent") is not False
            or status.get("initializationPresent") is not False
        ):
            raise BootstrapError("installer_result_ambiguous")
        return {**status, "reconciled": True}


INSTALLER_SOURCE = rb"""#!/usr/bin/env python3
from __future__ import annotations
import argparse, ctypes, errno, hashlib, json, os, re, shutil, stat, sys, time
from pathlib import Path

ROOT=Path("/homeassistant"); COMPONENT=ROOT/"custom_components"/"aurora_deploy"; TRUST=ROOT/"aurora_deploy_trusted_keys.json"; CONFIG=ROOT/"configuration.yaml"; LOCK=ROOT/".aurora-deploy-bootstrap-global.lock"
STAGE_PREFIX=".aurora-deploy-bootstrap-stage-"; TX_PREFIX=".aurora-deploy-bootstrap-transaction-"; TX_INIT_PREFIX=".aurora-deploy-bootstrap-transaction-init-"; INSTALLER="install-aurora-deploy-bootstrap.py"; MANIFEST="payload-manifest.json"; FILES=("__init__.py","adapter.py","manifest.json"); PAYLOAD=tuple(f"custom_components/aurora_deploy/{name}" for name in FILES)+("aurora_deploy_trusted_keys.json",)
SHA=re.compile(r"^[0-9a-f]{64}$"); NONCE=re.compile(r"^[0-9a-f]{32}$"); UUID=re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"); PYCACHE=re.compile(r"^__pycache__/(?:__init__|adapter)\.cpython-\d{3}(?:\.opt-[12])?\.pyc$"); STALE=3600; WINDOW=86400

def fail(code): raise RuntimeError(code)
def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":")).encode()
def pairs(values):
    result={}
    for key,value in values:
        if key in result: fail("json_duplicate_key")
        result[key]=value
    return result
def load(path,code):
    try: value=json.loads(path.read_bytes(),object_pairs_hook=pairs)
    except RuntimeError: raise
    except Exception: fail(code)
    if not isinstance(value,dict): fail(code)
    return value
def fsync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
def kind(path, expected, code):
    try: info=os.lstat(path)
    except FileNotFoundError: fail(code)
    if stat.S_ISLNK(info.st_mode) or (expected=="file" and not stat.S_ISREG(info.st_mode)) or (expected=="dir" and not stat.S_ISDIR(info.st_mode)): fail(code)
    return info
def lexists(path): return os.path.lexists(path)
def file_state(path):
    if not lexists(path): return {"exists":False}
    info=kind(path,"file","destination_invalid"); data=path.read_bytes()
    return {"exists":True,"sha256":digest(data),"size":len(data),"mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid}
def configuration_snapshot():
    info=kind(CONFIG,"file","destination_invalid"); data=CONFIG.read_bytes()
    return {"exists":True,"sha256":digest(data),"size":len(data),"mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid},data
def tree_state(path):
    if not lexists(path): return {"exists":False,"files":{},"directories":{}}
    root_info=kind(path,"dir","destination_component_invalid"); files={}; directories={".":{"mode":stat.S_IMODE(root_info.st_mode),"uid":root_info.st_uid,"gid":root_info.st_gid}}; count=0; total=0
    for base,dirs,names in os.walk(path,followlinks=False):
        basep=Path(base)
        if len(basep.relative_to(path).parts)>8: fail("component_tree_too_large")
        for name in dirs:
            info=kind(basep/name,"dir","destination_symlink_rejected"); directories[(basep/name).relative_to(path).as_posix()]={"mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid}; count+=1
        for name in names:
            candidate=basep/name; info=kind(candidate,"file","destination_symlink_rejected"); data=candidate.read_bytes(); total+=len(data); count+=1; files[candidate.relative_to(path).as_posix()]={"sha256":digest(data),"size":info.st_size,"mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid}
        if count>128 or total>8388608: fail("component_tree_too_large")
    return {"exists":True,"files":files,"directories":directories}
def component_without_generated(state):
    if not state["exists"]: return state
    files=dict(state["files"]); directories=dict(state["directories"])
    for name in tuple(files):
        if name.startswith("__pycache__/"):
            if not PYCACHE.fullmatch(name): fail("component_generated_artifact_invalid")
            del files[name]
    for name in tuple(directories):
        if name=="__pycache__": del directories[name]
        elif name.startswith("__pycache__/"): fail("component_generated_artifact_invalid")
    return {"exists":True,"files":files,"directories":directories}
def component_equal(first,second): return component_without_generated(first)==component_without_generated(second)
def content_tree(state):
    projected=component_without_generated(state)
    return digest(canonical({"exists":projected["exists"],"files":{key:value["sha256"] for key,value in projected.get("files",{}).items()},"directories":sorted(key for key in projected.get("directories",{}) if key!=".")}))
def state_equal(key,first,second): return component_equal(first,second) if key=="component" else first==second
def states_equal(first,second): return all(state_equal(key,first[key],second[key]) for key in ("configuration","component","trust"))
def states(): return {"configuration":file_state(CONFIG),"component":tree_state(COMPONENT),"trust":file_state(TRUST)}
def clean_atomic_temps(path):
    prefix=path.name+".new-"; matches=[item for item in path.parent.iterdir() if item.name.startswith(prefix)]
    if len(matches)>16: fail("temporary_path_count_invalid")
    for item in matches: kind(item,"file","temporary_path_invalid"); os.unlink(item)
    if matches: fsync_dir(path.parent)
def write_atomic(path,data,metadata=None):
    clean_atomic_temps(path); temp=path.with_name(path.name+f".new-{os.getpid()}-{time.time_ns()}")
    mode=metadata["mode"] if metadata else 0o600; flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0); fd=os.open(temp,flags,mode)
    try:
        view=memoryview(data)
        while view:
            written=os.write(fd,view)
            if written<=0: fail("atomic_write_failed")
            view=view[written:]
        os.fchmod(fd,mode)
        if metadata:
            try: os.fchown(fd,metadata["uid"],metadata["gid"])
            except PermissionError:
                current=os.fstat(fd)
                if current.st_uid!=metadata["uid"] or current.st_gid!=metadata["gid"]: fail("configuration_metadata_unpreserved")
        os.fsync(fd)
    finally: os.close(fd)
    os.replace(temp,path); fsync_dir(path.parent)
def write_journal(tx,value): write_atomic(tx/"transaction.json",canonical(value))
def lock_candidate(txid): return ROOT/f".aurora-deploy-bootstrap-lock-candidate-{txid}"
def process_record(pid):
    if sys.platform!="linux":
        try: os.kill(pid,0)
        except ProcessLookupError: return None
        except PermissionError: return ("R",0)
        return ("R",0)
    try: raw=Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError,ProcessLookupError): return None
    end=raw.rfind(")")
    if end<0: fail("process_identity_unavailable")
    fields=raw[end+2:].split()
    if len(fields)<20 or not fields[19].isdigit(): fail("process_identity_unavailable")
    return (fields[0],int(fields[19]))
def boot_identity():
    if sys.platform!="linux": return "00000000-0000-4000-8000-000000000000"
    try: value=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError: fail("process_identity_unavailable")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",value): fail("process_identity_unavailable")
    return value
def lock_value(txid,installer_sha256):
    process=process_record(os.getpid())
    if process is None or process[0] in {"Z","X"}: fail("process_identity_unavailable")
    return {"schemaVersion":"aurora-deploy-bootstrap-lock-v4","transactionId":txid,"acquiredAt":int(time.time()),"installerSha256":installer_sha256,"processId":os.getpid(),"processStartTicks":process[1],"bootId":boot_identity(),"probeNonce":os.urandom(16).hex()}
def valid_lock(path,code):
    value=load(path,code)
    if value.get("schemaVersion")!="aurora-deploy-bootstrap-lock-v4" or not UUID.fullmatch(str(value.get("transactionId",""))) or not isinstance(value.get("acquiredAt"),int) or not SHA.fullmatch(str(value.get("installerSha256",""))) or not isinstance(value.get("processId"),int) or isinstance(value.get("processId"),bool) or value.get("processId")<=0 or not isinstance(value.get("processStartTicks"),int) or isinstance(value.get("processStartTicks"),bool) or value.get("processStartTicks")<0 or not isinstance(value.get("bootId"),str) or not NONCE.fullmatch(str(value.get("probeNonce",""))): fail(code)
    return value
def lock_owner_alive(value):
    if value["bootId"]!=boot_identity(): return False
    process=process_record(value["processId"])
    return process is not None and process[0] not in {"Z","X"} and process[1]==value["processStartTicks"]
def cleanup_owned_lock_candidate(candidate,created):
    if not lexists(candidate): return
    current=kind(candidate,"file","global_lock_candidate_cleanup_failed")
    if current.st_dev!=created.st_dev or current.st_ino!=created.st_ino: fail("global_lock_candidate_cleanup_failed")
    os.unlink(candidate); fsync_dir(ROOT)
def acquire(txid,installer_sha256):
    candidate=lock_candidate(txid)
    if lexists(candidate): fail("global_lock_candidate_exists")
    value=lock_value(txid,installer_sha256); data=canonical(value); flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0); fd=os.open(candidate,flags,0o600)
    created=os.fstat(fd)
    try:
        try:
            view=memoryview(data)
            while view:
                written=os.write(fd,view)
                if written<=0: fail("global_lock_candidate_write_failed")
                view=view[written:]
            os.fsync(fd)
        finally: os.close(fd)
    except Exception:
        cleanup_owned_lock_candidate(candidate,created); raise
    published=False
    try:
        os.link(candidate,LOCK,follow_symlinks=False); published=True
        fsync_dir(ROOT); cleanup_owned_lock_candidate(candidate,created)
    except FileExistsError:
        cleanup_owned_lock_candidate(candidate,created); fail("global_lock_held")
    except Exception:
        if not published: cleanup_owned_lock_candidate(candidate,created)
        raise
    return value["probeNonce"]
def release(txid):
    lock=valid_lock(LOCK,"global_lock_invalid")
    if lock.get("transactionId")!=txid: fail("global_lock_owner_mismatch")
    candidate=lock_candidate(txid)
    if lexists(candidate):
        lock_info=kind(LOCK,"file","global_lock_invalid"); candidate_info=kind(candidate,"file","global_lock_candidate_invalid")
        if not os.path.samestat(lock_info,candidate_info): fail("global_lock_candidate_invalid")
        os.unlink(candidate); fsync_dir(ROOT)
    os.unlink(LOCK); fsync_dir(ROOT)
def read_payload(stage,expected):
    kind(stage,"dir","stage_missing"); actual={item.name for item in stage.iterdir()}; required={"custom_components","aurora_deploy_trusted_keys.json",MANIFEST,INSTALLER}
    if actual!=required: fail("stage_file_set_invalid")
    component=stage/"custom_components"/"aurora_deploy"; kind(component,"dir","stage_invalid")
    if {item.name for item in component.iterdir()}!=set(FILES): fail("stage_file_set_invalid")
    manifest_path=stage/MANIFEST; kind(manifest_path,"file","stage_symlink_rejected"); raw=manifest_path.read_bytes()
    if digest(raw)!=expected: fail("payload_manifest_hash_mismatch")
    manifest=load(manifest_path,"payload_manifest_invalid")
    if manifest.get("schemaVersion")!="aurora-deploy-bootstrap-v2" or not isinstance(manifest.get("files"),list): fail("payload_manifest_invalid")
    entries=manifest["files"]
    if [item.get("path") for item in entries if isinstance(item,dict)]!=list(PAYLOAD): fail("payload_manifest_invalid")
    result={}
    for item in entries:
        if not isinstance(item,dict) or set(item)!={"path","sha256","size"}: fail("payload_manifest_invalid")
        path=stage/item["path"]; info=kind(path,"file","stage_symlink_rejected"); data=path.read_bytes()
        if info.st_size!=item["size"] or digest(data)!=item["sha256"]: fail("payload_hash_mismatch")
        result[item["path"]]=data
    return result
def configuration_after(data):
    try: text=data.decode()
    except UnicodeDecodeError: fail("configuration_invalid")
    key=re.compile(r"^(?:aurora_deploy|[\"']aurora_deploy[\"'])[ \t]*:(?:\s|$)"); meaningful=[line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if any(line.startswith(("{","?")) for line in meaningful): fail("configuration_yaml_style_unsupported")
    count=sum(1 for line in text.splitlines() if key.match(line))
    if count>1: fail("configuration_key_duplicate")
    if count==1: return data
    return data+(b"" if not data or data.endswith(b"\n") else b"\n")+b"aurora_deploy:\n"
def assert_expected(current,args):
    if current["configuration"].get("sha256")!=args.expected_configuration_sha256: fail("configuration_drift")
    if content_tree(current["component"])!=args.expected_component_tree_sha256: fail("component_drift")
    trust=current["trust"].get("sha256","absent")
    if trust!=args.expected_trust_sha256: fail("trust_drift")
def exact(current,expected): return states_equal(current,expected)
def state_for(key,path): return tree_state(path) if key=="component" else file_state(path)
def rename_noreplace(source,destination,code):
    if sys.platform=="linux":
        libc=ctypes.CDLL(None,use_errno=True); result=libc.syscall(ctypes.c_long(316),ctypes.c_int(-100),ctypes.c_char_p(os.fsencode(source)),ctypes.c_int(-100),ctypes.c_char_p(os.fsencode(destination)),ctypes.c_uint(1))
        if result==0: return
        error=ctypes.get_errno()
        if error in {errno.EEXIST,errno.ENOTEMPTY}: fail(code)
        if error in {errno.ENOSYS,errno.EINVAL}: fail("atomic_rename_unsupported")
        raise OSError(error,os.strerror(error))
    if lexists(destination): fail(code)
    os.rename(source,destination)
def create_runtime_probe(path,marker):
    if lexists(path): fail("runtime_probe_residue")
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0); fd=os.open(path,flags,0o600); created=os.fstat(fd)
    try:
        try:
            view=memoryview(marker)
            while view:
                written=os.write(fd,view)
                if written<=0: fail("runtime_probe_write_failed")
                view=view[written:]
            os.fsync(fd)
        finally: os.close(fd)
    except Exception:
        if lexists(path):
            current=kind(path,"file","runtime_probe_invalid")
            if current.st_dev!=created.st_dev or current.st_ino!=created.st_ino: fail("runtime_probe_invalid")
            os.unlink(path); fsync_dir(ROOT)
        raise
    fsync_dir(ROOT); return created
def runtime_probe_owned(path,created,marker):
    if not lexists(path): return False
    current=kind(path,"file","runtime_probe_invalid")
    return current.st_dev==created.st_dev and current.st_ino==created.st_ino and path.read_bytes()==marker
def cleanup_owned_runtime_probe(path,created,marker):
    if not lexists(path): return False
    if not runtime_probe_owned(path,created,marker): return False
    os.unlink(path); fsync_dir(ROOT); return True
def require_runtime_primitives(txid,nonce):
    if ROOT!=Path("/homeassistant"): return
    if sys.platform!="linux": fail("linux_runtime_required")
    if not NONCE.fullmatch(nonce): fail("runtime_probe_invalid")
    source=ROOT/f".aurora-deploy-bootstrap-runtime-source-{txid}-{nonce}"; destination=ROOT/f".aurora-deploy-bootstrap-runtime-destination-{txid}-{nonce}"; link_source=ROOT/f".aurora-deploy-bootstrap-runtime-link-source-{txid}-{nonce}"; link_destination=ROOT/f".aurora-deploy-bootstrap-runtime-link-destination-{txid}-{nonce}"; marker=canonical({"transactionId":txid,"probeNonce":nonce})
    if any(lexists(path) for path in (source,destination,link_source,link_destination)): fail("runtime_probe_residue")
    created=create_runtime_probe(source,marker); renamed=False
    try:
        rename_noreplace(source,destination,"runtime_probe_destination_exists"); renamed=True; fsync_dir(ROOT)
        if not runtime_probe_owned(destination,created,marker): fail("runtime_probe_invalid")
    finally:
        cleanup_owned_runtime_probe(source,created,marker)
        destination_owned=cleanup_owned_runtime_probe(destination,created,marker)
        if renamed and not destination_owned: fail("runtime_probe_invalid")
    linked_created=create_runtime_probe(link_source,marker); linked=False
    try:
        try: os.link(link_source,link_destination,follow_symlinks=False); linked=True
        except FileExistsError: fail("runtime_probe_destination_exists")
        fsync_dir(ROOT)
        if not runtime_probe_owned(link_source,linked_created,marker) or not runtime_probe_owned(link_destination,linked_created,marker): fail("runtime_probe_invalid")
    finally:
        source_owned=cleanup_owned_runtime_probe(link_source,linked_created,marker)
        destination_owned=cleanup_owned_runtime_probe(link_destination,linked_created,marker)
        if linked and (not source_owned or not destination_owned): fail("runtime_probe_invalid")
def cleanup_runtime_probe(txid,nonce):
    if not NONCE.fullmatch(nonce): fail("runtime_probe_invalid")
    marker=canonical({"transactionId":txid,"probeNonce":nonce})
    for path in (ROOT/f".aurora-deploy-bootstrap-runtime-source-{txid}-{nonce}",ROOT/f".aurora-deploy-bootstrap-runtime-destination-{txid}-{nonce}",ROOT/f".aurora-deploy-bootstrap-runtime-link-source-{txid}-{nonce}",ROOT/f".aurora-deploy-bootstrap-runtime-link-destination-{txid}-{nonce}"):
        if lexists(path):
            kind(path,"file","runtime_probe_invalid")
            data=path.read_bytes()
            if len(data)>len(marker) or data!=marker[:len(data)]: fail("runtime_probe_invalid")
            os.unlink(path)
    fsync_dir(ROOT)
def fsync_move(source,destination):
    fsync_dir(source.parent)
    if destination.parent!=source.parent: fsync_dir(destination.parent)
def move_verified(key,source,destination,expected,drift_code):
    if lexists(destination): fail("recovery_path_exists")
    if not lexists(source): fail(drift_code)
    rename_noreplace(source,destination,"recovery_path_exists"); fsync_move(source,destination)
    if not state_equal(key,state_for(key,destination),expected):
        if not lexists(source): rename_noreplace(destination,source,"destination_recreated"); fsync_move(destination,source)
        fail(drift_code)
def publish_verified(key,source,destination,expected):
    rename_noreplace(source,destination,f"{key}_destination_recreated"); fsync_move(source,destination)
    if not state_equal(key,state_for(key,destination),expected): fail("post_write_readback_failed")
def previous_path(tx,key): return tx/("previous-configuration.yaml" if key=="configuration" else f"previous-{key}")
def displaced_path(tx,key): return tx/f"rollback-displaced-{key}"
def verify_prestate_artifacts(tx,journal,current):
    pre=journal["prestate"]; installed=journal.get("installed"); status=journal.get("status")
    if not isinstance(pre,dict) or not isinstance(installed,dict) or set(pre)!={"configuration","component","trust"} or set(installed)!=set(pre): fail("transaction_invalid")
    if exact(current,pre): return "already"
    for key in ("component","trust","configuration"):
        expected=pre[key]; live=current[key]; previous=previous_path(tx,key); displaced=displaced_path(tx,key); displaced_valid=False
        if lexists(displaced):
            if not state_equal(key,state_for(key,displaced),installed[key]): fail("recovery_artifact_hash_mismatch")
            displaced_valid=True
        previous_valid=False
        if expected["exists"]:
            if lexists(previous):
                if not state_equal(key,state_for(key,previous),expected): fail("recovery_artifact_hash_mismatch")
                previous_valid=True
        elif lexists(previous): fail("unexpected_recovery_artifact")
        if state_equal(key,live,expected): continue
        if state_equal(key,live,installed[key]): pass
        elif live.get("exists") is False and status=="prepared" and previous_valid: pass
        elif live.get("exists") is False and status=="rollback_prepared" and displaced_valid: pass
        else: fail("rollback_destination_drift")
        if expected["exists"] and not previous_valid: fail("recovery_artifact_missing")
    return "restore"
def restore(tx,journal):
    current=states(); action=verify_prestate_artifacts(tx,journal,current)
    if action=="already": return
    pre=journal["prestate"]; installed=journal["installed"]
    for key,destination in (("component",COMPONENT),("trust",TRUST),("configuration",CONFIG)):
        live=state_for(key,destination)
        if state_equal(key,live,pre[key]): continue
        displaced=displaced_path(tx,key)
        if state_equal(key,live,installed[key]): move_verified(key,destination,displaced,installed[key],"rollback_destination_drift")
        elif not (live.get("exists") is False and journal.get("status") in {"prepared","rollback_prepared"}): fail("rollback_destination_drift")
        previous=previous_path(tx,key)
        if pre[key]["exists"]: publish_verified(key,previous,destination,pre[key])
        elif lexists(destination): fail("rollback_destination_recreated")
        fsync_dir(destination.parent); fsync_dir(tx)
    if not states_equal(states(),pre): fail("rollback_readback_failed")
def install_destination(key,destination,replacement,tx,pre,installed):
    previous=previous_path(tx,key)
    if pre[key]["exists"]: move_verified(key,destination,previous,pre[key],f"{key}_drift")
    elif lexists(destination): fail(f"{key}_drift")
    publish_verified(key,replacement,destination,installed[key])
def install(args,stage,tx,installer_bytes):
    kind(ROOT,"dir","config_root_invalid"); kind(ROOT/"custom_components","dir","custom_components_invalid"); payload=read_payload(stage,args.payload_manifest_sha256); config_state,config_bytes=configuration_snapshot(); current={"configuration":config_state,"component":tree_state(COMPONENT),"trust":file_state(TRUST)}; assert_expected(current,args)
    desired_files={name:digest(payload[f"custom_components/aurora_deploy/{name}"]) for name in FILES}; desired_tree=digest(canonical({"exists":True,"files":desired_files,"directories":[]})); divergent=current["component"]["exists"] and content_tree(current["component"])!=desired_tree; trust_divergent=current["trust"]["exists"] and current["trust"]["sha256"]!=digest(payload["aurora_deploy_trusted_keys.json"])
    if (divergent or trust_divergent) and not args.replace_exact: fail("replace_exact_required")
    initialization=ROOT/f"{TX_INIT_PREFIX}{args.transaction_id}"
    if lexists(tx) or lexists(initialization): fail("transaction_exists")
    initialization.mkdir(mode=0o700); fsync_dir(ROOT); write_atomic(initialization/INSTALLER,installer_bytes)
    if digest((initialization/INSTALLER).read_bytes())!=args.expected_installer_sha256: fail("installer_hash_mismatch")
    config_data=configuration_after(config_bytes); new_component=initialization/"new-component"; new_component.mkdir(mode=0o755); fsync_dir(initialization)
    for name in FILES: write_atomic(new_component/name,payload[f"custom_components/aurora_deploy/{name}"],{"mode":0o644,"uid":os.getuid(),"gid":os.getgid()})
    new_trust=initialization/"new-trust"; write_atomic(new_trust,payload["aurora_deploy_trusted_keys.json"])
    new_config=initialization/"new-configuration.yaml"; write_atomic(new_config,config_data,current["configuration"])
    installed={"component":tree_state(new_component),"trust":file_state(new_trust),"configuration":file_state(new_config)}; now=int(time.time()); journal={"schemaVersion":"aurora-deploy-bootstrap-transaction-v2","transactionId":args.transaction_id,"payloadManifestSha256":args.payload_manifest_sha256,"installerSha256":digest((initialization/INSTALLER).read_bytes()),"replaceExact":args.replace_exact,"status":"prepared","createdAt":now,"rollbackDeadline":now+WINDOW,"prestate":current,"installed":installed}
    write_journal(initialization,journal); rename_noreplace(initialization,tx,"transaction_exists"); fsync_dir(ROOT); new_component=tx/"new-component"; new_trust=tx/"new-trust"; new_config=tx/"new-configuration.yaml"; assert_expected(states(),args)
    try:
        install_destination("component",COMPONENT,new_component,tx,current,installed)
        install_destination("trust",TRUST,new_trust,tx,current,installed)
        install_destination("configuration",CONFIG,new_config,tx,current,installed)
        if not states_equal(states(),installed): fail("post_write_readback_failed")
        journal["status"]="installed"; journal["configurationSha256"]=installed["configuration"]["sha256"]; journal["componentTreeSha256"]=content_tree(installed["component"]); journal["trustSha256"]=installed["trust"]["sha256"]; write_journal(tx,journal)
    except Exception:
        try: restore(tx,journal); journal["status"]="rolled_back_after_failure"; journal["configurationSha256"]=journal["prestate"]["configuration"]["sha256"]; journal["componentTreeSha256"]=content_tree(journal["prestate"]["component"]); journal["trustSha256"]=journal["prestate"]["trust"].get("sha256","absent"); write_journal(tx,journal)
        except Exception: fail("partial_failure_rollback_failed")
        fail("install_failed_rolled_back")
    return journal
def cleanup_initialization(txid):
    initialization=ROOT/f"{TX_INIT_PREFIX}{txid}"
    if not lexists(initialization): return
    kind(initialization,"dir","transaction_initialization_invalid"); tree_state(initialization); shutil.rmtree(initialization); fsync_dir(ROOT)
def validate_recovery_pins(journal,args):
    for value in (args.expected_configuration_sha256,args.expected_component_tree_sha256,args.payload_manifest_sha256):
        if not isinstance(value,str) or not SHA.fullmatch(value): fail("recovery_pin_invalid")
    if args.expected_trust_sha256!="absent" and (not isinstance(args.expected_trust_sha256,str) or not SHA.fullmatch(args.expected_trust_sha256)): fail("recovery_pin_invalid")
    pre=journal.get("prestate")
    if not isinstance(pre,dict) or journal.get("payloadManifestSha256")!=args.payload_manifest_sha256 or journal.get("replaceExact") is not args.replace_exact or pre.get("configuration",{}).get("sha256")!=args.expected_configuration_sha256 or content_tree(pre.get("component",{}))!=args.expected_component_tree_sha256 or pre.get("trust",{}).get("sha256","absent")!=args.expected_trust_sha256: fail("recovery_pin_mismatch")
def recover(txid,tx,args):
    expected_installer_sha256=args.expected_installer_sha256
    candidate=lock_candidate(txid)
    if not lexists(LOCK):
        info=kind(candidate,"file","global_lock_missing")
        if time.time()-info.st_mtime<STALE: fail("global_lock_not_stale")
        os.unlink(candidate); fsync_dir(ROOT); return {"status":"lock_recovered","transactionId":txid}
    kind(LOCK,"file","global_lock_invalid"); lock=valid_lock(LOCK,"global_lock_invalid")
    if lock.get("transactionId")!=txid or lock.get("installerSha256")!=expected_installer_sha256 or time.time()-lock["acquiredAt"]<STALE: fail("global_lock_not_stale")
    if lock_owner_alive(lock): fail("global_lock_owner_still_running")
    if lexists(candidate):
        lock_info=kind(LOCK,"file","global_lock_invalid"); candidate_info=kind(candidate,"file","global_lock_candidate_invalid")
        if not os.path.samestat(lock_info,candidate_info):
            if time.time()-candidate_info.st_mtime<STALE: fail("global_lock_candidate_invalid")
            os.unlink(candidate); fsync_dir(ROOT)
    cleanup_runtime_probe(txid,lock["probeNonce"])
    if lexists(tx):
        journal=load(tx/"transaction.json","transaction_invalid")
        if journal.get("installerSha256")!=expected_installer_sha256: fail("installer_hash_mismatch")
        validate_recovery_pins(journal,args)
        if journal.get("status") in {"prepared","rollback_prepared"}: restore(tx,journal); journal["status"]="rolled_back_after_recovery"; journal["configurationSha256"]=journal["prestate"]["configuration"]["sha256"]; journal["componentTreeSha256"]=content_tree(journal["prestate"]["component"]); journal["trustSha256"]=journal["prestate"]["trust"].get("sha256","absent"); write_journal(tx,journal)
        elif journal.get("status") not in {"installed","rolled_back","rolled_back_after_failure","rolled_back_after_recovery","restart_verified","finalize_prepared","rollback_finalize_prepared"}: fail("lock_recovery_state_unsafe")
    else: cleanup_initialization(txid)
    release(txid); return {"status":"lock_recovered","transactionId":txid}
def receipt(status,txid,journal): return {"status":status,"transactionId":txid,"payloadManifestSha256":journal["payloadManifestSha256"],"configurationSha256":journal.get("configurationSha256",journal["prestate"]["configuration"]["sha256"]),"componentTreeSha256":journal.get("componentTreeSha256",content_tree(journal["prestate"]["component"])),"trustSha256":journal.get("trustSha256",journal["prestate"]["trust"].get("sha256","absent")),"rollbackDeadline":journal["rollbackDeadline"]}
def main():
    parser=argparse.ArgumentParser(add_help=False); parser.add_argument("mode",choices=("install","rollback","recover-lock","abort-stage","mark-verified","finalize")); parser.add_argument("--transaction-id",required=True); parser.add_argument("--expected-configuration-sha256"); parser.add_argument("--expected-component-tree-sha256"); parser.add_argument("--expected-trust-sha256"); parser.add_argument("--payload-manifest-sha256"); parser.add_argument("--expected-installer-sha256"); parser.add_argument("--replace-exact",action="store_true"); args=parser.parse_args()
    if not UUID.fullmatch(args.transaction_id): fail("argument_invalid")
    installer_bytes=Path(__file__).read_bytes()
    if not isinstance(args.expected_installer_sha256,str) or not SHA.fullmatch(args.expected_installer_sha256) or digest(installer_bytes)!=args.expected_installer_sha256: fail("installer_hash_mismatch")
    stage=ROOT/f"{STAGE_PREFIX}{args.transaction_id}"; tx=ROOT/f"{TX_PREFIX}{args.transaction_id}"
    if args.mode=="recover-lock": result=recover(args.transaction_id,tx,args)
    else:
        probe_nonce=acquire(args.transaction_id,args.expected_installer_sha256)
        release_required=True
        try:
            if args.mode!="abort-stage": require_runtime_primitives(args.transaction_id,probe_nonce)
            try:
                if args.mode=="install":
                    for value in (args.expected_configuration_sha256,args.expected_component_tree_sha256,args.payload_manifest_sha256,args.expected_installer_sha256):
                        if not isinstance(value,str) or not SHA.fullmatch(value): fail("argument_invalid")
                    if args.expected_trust_sha256!="absent" and (not isinstance(args.expected_trust_sha256,str) or not SHA.fullmatch(args.expected_trust_sha256)): fail("argument_invalid")
                    journal=install(args,stage,tx,installer_bytes); result=receipt("installed",args.transaction_id,journal)
                elif args.mode=="abort-stage":
                    if lexists(tx): fail("stage_abort_transaction_exists")
                    cleanup_initialization(args.transaction_id); kind(stage,"dir","stage_missing"); shutil.rmtree(stage); fsync_dir(ROOT); result={"status":"stage_aborted","transactionId":args.transaction_id}
                else:
                    kind(tx,"dir","transaction_missing"); journal=load(tx/"transaction.json","transaction_invalid")
                    if journal.get("transactionId")!=args.transaction_id: fail("transaction_invalid")
                    if journal.get("installerSha256")!=args.expected_installer_sha256: fail("installer_hash_mismatch")
                    validate_recovery_pins(journal,args)
                    if args.mode=="rollback":
                        if journal.get("status") not in {"installed","prepared","rolled_back_after_failure","rolled_back_after_recovery"}: fail("rollback_state_invalid")
                        verify_prestate_artifacts(tx,journal,states()); journal["status"]="rollback_prepared"; journal["configurationSha256"]=journal["installed"]["configuration"]["sha256"]; journal["componentTreeSha256"]=content_tree(journal["installed"]["component"]); journal["trustSha256"]=journal["installed"]["trust"]["sha256"]; write_journal(tx,journal)
                        try: restore(tx,journal)
                        except Exception: fail("partial_failure_rollback_failed")
                        journal["status"]="rolled_back"; journal["configurationSha256"]=journal["prestate"]["configuration"]["sha256"]; journal["componentTreeSha256"]=content_tree(journal["prestate"]["component"]); journal["trustSha256"]=journal["prestate"]["trust"].get("sha256","absent"); write_journal(tx,journal); result=receipt("rolled_back",args.transaction_id,journal)
                    elif args.mode=="mark-verified":
                        if journal.get("status") not in {"installed","restart_verified"} or not states_equal(states(),journal["installed"]): fail("restart_readback_mismatch")
                        journal["status"]="restart_verified"; write_journal(tx,journal); result=receipt("restart_verified",args.transaction_id,journal)
                    else:
                        rollback_cleanup=journal.get("status") in {"rolled_back","rolled_back_after_failure","rolled_back_after_recovery","rollback_finalize_prepared"}
                        if rollback_cleanup:
                            if not states_equal(states(),journal["prestate"]): fail("finalize_not_allowed")
                            if journal.get("status")!="rollback_finalize_prepared": journal["status"]="rollback_finalize_prepared"; write_journal(tx,journal)
                        else:
                            if journal.get("status") not in {"restart_verified","finalize_prepared"} or time.time()<journal["rollbackDeadline"] or not states_equal(states(),journal["installed"]): fail("finalize_not_allowed")
                            if journal.get("status")!="finalize_prepared": journal["status"]="finalize_prepared"; write_journal(tx,journal)
                        result={"status":"finalized","transactionId":args.transaction_id,"payloadManifestSha256":journal["payloadManifestSha256"]}
                        if lexists(stage): kind(stage,"dir","stage_invalid"); shutil.rmtree(stage); fsync_dir(ROOT)
                        release(args.transaction_id); release_required=False
                        shutil.rmtree(tx); fsync_dir(ROOT)
            except Exception as error:
                if str(error)=="partial_failure_rollback_failed": release_required=False
                raise
        finally:
            if release_required: release(args.transaction_id)
    print(json.dumps(result,sort_keys=True,separators=(",",":"))); return 0
if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as error:
        code=str(error) if isinstance(error,RuntimeError) and re.fullmatch(r"[a-z0-9_]{1,96}",str(error)) else "operation_failed"
        print(json.dumps({"status":"failed","error":code},sort_keys=True,separators=(",",":")),file=sys.stderr); raise SystemExit(1)
"""
INSTALLER_SHA256 = _digest(INSTALLER_SOURCE)


async def _supervisor_snapshot(
    url: str, token: str, socket_factory: Any
) -> dict[str, Any]:
    async with socket_factory(url, token) as socket:
        calls = {
            "user": await socket.call("auth/current_user"),
            "supervisor": await socket.call(
                "supervisor/api", endpoint="/supervisor/info", method="get"
            ),
            "host": await socket.call(
                "supervisor/api", endpoint="/host/info", method="get"
            ),
            "backup": await socket.call("backup/info"),
            "addon": await socket.call(
                "supervisor/api",
                endpoint=f"/addons/{FILE_EDITOR_SLUG}/info",
                method="get",
            ),
            "store": await socket.call(
                "supervisor/api",
                endpoint=f"/store/addons/{FILE_EDITOR_SLUG}",
                method="get",
            ),
        }
    return {key: _supervisor_data(value) for key, value in calls.items()}


async def _addon_action(url: str, token: str, action: str, socket_factory: Any) -> None:
    async with socket_factory(url, token) as socket:
        await socket.call(
            "supervisor/api",
            endpoint=f"/addons/{FILE_EDITOR_SLUG}/{action}",
            method="post",
        )
        expected = "started" if action == "start" else "stopped"
        for _ in range(120):
            value = _supervisor_data(
                await socket.call(
                    "supervisor/api",
                    endpoint=f"/addons/{FILE_EDITOR_SLUG}/info",
                    method="get",
                )
            )
            if value.get("state") == expected:
                return
            await asyncio.sleep(0.25)
    raise BootstrapError("file_editor_lifecycle_unverified")


async def _open_editor(  # noqa: C901
    url: str,
    token: str,
    backup_id: str | None,
    expected_backup_agent_id: str | None,
    require_backup: bool,
    operation_mode: str,
    transaction_id: str | None,
    socket_factory: Any,
) -> tuple[str, str, str]:
    snapshot = await _supervisor_snapshot(url, token, socket_factory)
    observed_initial = snapshot["addon"].get("state")
    initial = observed_initial
    ingress = validate_supervisor_preflight(
        **snapshot,
        backup_id=backup_id,
        expected_backup_agent_id=expected_backup_agent_id,
        require_backup=require_backup,
    )
    lease = _read_lifecycle_lease()
    adopted = False
    if lease is not None:
        if _lifecycle_process_alive(lease["processId"]):
            raise BootstrapError("file_editor_lifecycle_lease_active")
        if lease["initialState"] == "started":
            if observed_initial == "stopped":
                await _addon_action(url, token, "start", socket_factory)
            _clear_lifecycle_lease()
            snapshot = await _supervisor_snapshot(url, token, socket_factory)
            observed_initial = snapshot["addon"].get("state")
            initial = observed_initial
            ingress = validate_supervisor_preflight(
                **snapshot,
                backup_id=backup_id,
                expected_backup_agent_id=expected_backup_agent_id,
                require_backup=require_backup,
            )
        elif observed_initial == "started":
            _write_lifecycle_lease(operation_mode, transaction_id, create=False)
            initial = "stopped"
            adopted = True
        else:
            _clear_lifecycle_lease()
    started_by_us = initial == "stopped"
    restore_started_on_failure = False
    try:
        recovery_cycled = False
        if operation_mode == "recover-lock" and observed_initial == "started":
            if not adopted:
                _write_lifecycle_lease(
                    operation_mode,
                    transaction_id,
                    create=True,
                    initial_state="started",
                )
                restore_started_on_failure = True
            await _addon_action(url, token, "stop", socket_factory)
            await _addon_action(url, token, "start", socket_factory)
            recovery_cycled = True
            if restore_started_on_failure:
                _clear_lifecycle_lease()
                restore_started_on_failure = False
        if started_by_us:
            if not adopted and not recovery_cycled:
                _write_lifecycle_lease(operation_mode, transaction_id, create=True)
                await _addon_action(url, token, "start", socket_factory)
        if started_by_us or recovery_cycled:
            snapshot = await _supervisor_snapshot(url, token, socket_factory)
            ingress = validate_supervisor_preflight(
                **snapshot,
                backup_id=backup_id,
                expected_backup_agent_id=expected_backup_agent_id,
                require_backup=require_backup,
            )
            if snapshot["addon"].get("state") != "started":
                raise BootstrapError("file_editor_start_unverified")
        async with socket_factory(url, token) as socket:
            session = _supervisor_data(
                await socket.call(
                    "supervisor/api",
                    endpoint="/ingress/session",
                    method="post",
                    data={},
                )
            ).get("session")
        if not isinstance(session, str) or SAFE_SESSION.fullmatch(session) is None:
            raise BootstrapError("ingress_session_invalid")
        return initial, ingress, session
    except BaseException as primary:
        if restore_started_on_failure:
            try:
                await _addon_action(url, token, "start", socket_factory)
                _clear_lifecycle_lease()
            except BaseException:
                raise BootstrapError(
                    "operation_failed_and_file_editor_restore_failed"
                ) from None
        elif started_by_us:
            try:
                await _addon_action(url, token, "stop", socket_factory)
                _clear_lifecycle_lease()
            except BaseException:
                raise BootstrapError(
                    "operation_failed_and_file_editor_restore_failed"
                ) from None
        raise primary


def _stage_payload(
    client: FileEditorIngressClient, txid: str, payload: Payload
) -> None:
    client.create_stage(txid)
    # Store metadata does not expose the add-on mount map on current HAOS.
    # Prove the fixed root is writable using a transaction-owned marker, exact
    # readback, and deletion before any installer execution.
    client.verify_fixed_root_write_capability(txid)
    # The trusted installer is first so every later partial upload can be
    # resumed or explicitly removed. Existing bytes must match; source drift
    # never silently overwrites a partially staged transaction.
    staged = (
        (INSTALLER_NAME, INSTALLER_SOURCE, INSTALLER_SHA256),
        *((item.relative_path, item.content, item.sha256) for item in payload.files),
        (PAYLOAD_MANIFEST_NAME, payload.manifest, payload.manifest_sha256),
    )
    for relative, content, expected in staged:
        if client.stage_file_exists(txid, relative):
            if _digest(client.download_stage(txid, relative)) != expected:
                raise BootstrapError("remote_stage_conflict")
        else:
            client.upload(txid, relative, content)
        if _digest(client.download_stage(txid, relative)) != expected:
            raise BootstrapError("remote_stage_readback_mismatch")


def _ensure_stage_abort_installer(
    client: FileEditorIngressClient,
    txid: str,
    expected_installer_sha256: str,
    installer_bytes: bytes,
) -> None:
    client.create_stage(txid)
    if _digest(installer_bytes) != expected_installer_sha256:
        raise BootstrapError("local_installer_pin_mismatch")
    if (
        not client.stage_file_exists(txid, INSTALLER_NAME)
        or _digest(client.download_stage(txid, INSTALLER_NAME))
        != expected_installer_sha256
    ):
        client.upload(txid, INSTALLER_NAME, installer_bytes)
    if (
        _digest(client.download_stage(txid, INSTALLER_NAME))
        != expected_installer_sha256
    ):
        raise BootstrapError("remote_installer_hash_mismatch")


def _write_local_record(  # noqa: C901
    txid: str, value: dict[str, Any], *, create: bool
) -> None:
    directory = APPROVED_STATE_DIRECTORY
    local_root = directory.parent
    if local_root.exists() and local_root.is_symlink():
        raise BootstrapError("local_state_invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_info = directory.lstat()
    if (
        stat.S_ISLNK(directory_info.st_mode)
        or not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_IMODE(directory_info.st_mode) != 0o700
    ):
        raise BootstrapError("local_state_invalid")
    path = directory / f"{txid}.json"
    if os.path.lexists(path):
        path_info = path.lstat()
        if (
            stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or stat.S_IMODE(path_info.st_mode) != 0o600
        ):
            raise BootstrapError("local_state_invalid")
        if create:
            raise BootstrapError("local_transaction_exists")
    elif not create:
        raise BootstrapError("local_transaction_missing")
    prefix = f".{txid}.new-"
    residue = [item for item in directory.iterdir() if item.name.startswith(prefix)]
    if len(residue) > 128:
        raise BootstrapError("local_state_invalid")
    residue_pattern = re.compile(rf"^{re.escape(prefix)}([1-9][0-9]*)-[0-9a-f]{{16}}$")
    for item in residue:
        info = item.lstat()
        match = residue_pattern.fullmatch(item.name)
        if (
            match is None
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BootstrapError("local_state_invalid")
        process_id = int(match.group(1))
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            item.unlink()
        except PermissionError:
            pass
    temp = directory / f"{prefix}{os.getpid()}-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    temp_info = os.fstat(fd)
    try:
        try:
            remaining = memoryview(_canonical(value))
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise BootstrapError("local_state_write_failed")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if create:
            try:
                os.link(temp, path, follow_symlinks=False)
            except FileExistsError:
                raise BootstrapError("local_transaction_exists") from None
        else:
            os.replace(temp, path)
        _fsync_local_directory(directory)
    finally:
        if os.path.lexists(temp):
            current = temp.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or current.st_dev != temp_info.st_dev
                or current.st_ino != temp_info.st_ino
            ):
                raise BootstrapError("local_state_invalid") from None
            temp.unlink()
            _fsync_local_directory(directory)


def _fsync_local_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_local_record(txid: str) -> dict[str, Any]:
    path = APPROVED_STATE_DIRECTORY / f"{txid}.json"
    try:
        info = path.lstat()
    except OSError:
        raise BootstrapError("local_transaction_missing") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise BootstrapError("local_state_invalid")
    return _json_object(path.read_bytes(), "local_state_invalid")


def _prepare_install_record(txid: str, value: dict[str, Any]) -> None:
    """Atomically create, or safely resume, one identically pinned install."""
    try:
        _write_local_record(txid, value, create=True)
        return
    except BootstrapError as error:
        if str(error) != "local_transaction_exists":
            raise
    existing = _read_local_record(txid)
    immutable = {
        "schemaVersion",
        "transactionId",
        "payloadManifestSha256",
        "sourceRevision",
        "sourceRoot",
        "installerSha256",
        "installerSourceBase64",
        "replaceExact",
        "expectedConfigurationSha256",
        "expectedComponentTreeSha256",
        "expectedTrustSha256",
    }
    if existing.get("status") not in {"prepared", "installed"} or {
        key: existing.get(key) for key in immutable
    } != {key: value.get(key) for key in immutable}:
        raise BootstrapError("local_transaction_collision") from None


def _persist_local_request(txid: str, mode: str) -> dict[str, str | bool]:
    local = _read_local_record(txid)
    if (
        local.get("schemaVersion") != "aurora-deploy-bootstrap-local-v1"
        or local.get("transactionId") != txid
    ):
        raise BootstrapError("local_state_invalid")
    installer_sha256 = _validate_hash(
        local.get("installerSha256"), "local_installer_pin_required"
    )
    _local_installer_bytes(local)
    payload_manifest_sha256 = _validate_hash(
        local.get("payloadManifestSha256"), "local_payload_pin_required"
    )
    configuration_sha256 = _validate_hash(
        local.get("expectedConfigurationSha256"), "local_prestate_pin_required"
    )
    component_tree_sha256 = _validate_hash(
        local.get("expectedComponentTreeSha256"), "local_prestate_pin_required"
    )
    trust_sha256 = _validate_hash(
        local.get("expectedTrustSha256"),
        "local_prestate_pin_required",
        absent=True,
    )
    replace_exact = local.get("replaceExact")
    if not isinstance(replace_exact, bool):
        raise BootstrapError("local_replace_exact_pin_required")
    if not (
        mode == "finalize"
        and local.get("status") in {"finalize_authorized", "finalized"}
    ):
        local["status"] = f"{mode}_requested"
        _write_local_record(txid, local, create=False)
    result: dict[str, str | bool] = {
        "expected-installer-sha256": installer_sha256,
        "payload-manifest-sha256": payload_manifest_sha256,
        "expected-configuration-sha256": configuration_sha256,
        "expected-component-tree-sha256": component_tree_sha256,
        "expected-trust-sha256": trust_sha256,
    }
    if replace_exact:
        result["replace-exact"] = True
    return result


def _local_installer_bytes(local: dict[str, Any]) -> bytes:
    encoded = local.get("installerSourceBase64")
    if not isinstance(encoded, str) or len(encoded) > 256 * 1024:
        raise BootstrapError("local_installer_bytes_invalid")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise BootstrapError("local_installer_bytes_invalid") from None
    if base64.b64encode(content).decode() != encoded or _digest(
        content
    ) != _validate_hash(local.get("installerSha256"), "local_installer_pin_required"):
        raise BootstrapError("local_installer_pin_mismatch")
    return content


def _bind_status_to_local(
    status: dict[str, Any], txid: str, *, required: bool
) -> dict[str, Any]:
    path = APPROVED_STATE_DIRECTORY / f"{txid}.json"
    if not path.exists() and not path.is_symlink():
        if required:
            raise BootstrapError("local_transaction_missing")
        return status
    local = _read_local_record(txid)
    if (
        local.get("schemaVersion") != "aurora-deploy-bootstrap-local-v1"
        or local.get("transactionId") != txid
        or status.get("transactionId") != txid
    ):
        raise BootstrapError("local_state_invalid")
    transaction_present = status.get("transactionPresent") is True
    artifact_only = {
        "not_found": (False, False, False),
        "staged_partial": (True, False, False),
        "lock_candidate": (False, True, False),
        "initializing": (False, False, True),
    }
    if transaction_present and (
        status.get("installerSha256")
        != _validate_hash(local.get("installerSha256"), "local_installer_pin_required")
        or status.get("payloadManifestSha256")
        != _validate_hash(
            local.get("payloadManifestSha256"), "local_payload_pin_required"
        )
        or status.get("prestateConfigurationSha256")
        != _validate_hash(
            local.get("expectedConfigurationSha256"),
            "local_prestate_pin_required",
        )
        or status.get("prestateComponentTreeSha256")
        != _validate_hash(
            local.get("expectedComponentTreeSha256"),
            "local_prestate_pin_required",
        )
        or status.get("prestateTrustSha256")
        != _validate_hash(
            local.get("expectedTrustSha256"),
            "local_prestate_pin_required",
            absent=True,
        )
        or status.get("replaceExact") is not local.get("replaceExact")
    ):
        raise BootstrapError("remote_local_pin_mismatch")
    if not transaction_present:
        expected_topology = artifact_only.get(status.get("status"))
        if (
            expected_topology is None
            or status.get("transactionPresent") is not False
            or (
                status.get("stagePresent"),
                status.get("lockCandidatePresent"),
                status.get("initializationPresent"),
            )
            != expected_topology
            or any(
                key in status
                for key in {
                    "installerSha256",
                    "payloadManifestSha256",
                    "prestateConfigurationSha256",
                    "prestateComponentTreeSha256",
                    "prestateTrustSha256",
                    "replaceExact",
                }
            )
        ):
            raise BootstrapError("remote_local_pin_mismatch")
    return status


LIFECYCLE_RECORD_ID = "file-editor-lifecycle"


def _read_lifecycle_lease() -> dict[str, Any] | None:
    path = APPROVED_STATE_DIRECTORY / f"{LIFECYCLE_RECORD_ID}.json"
    if not path.exists() and not path.is_symlink():
        return None
    lease = _read_local_record(LIFECYCLE_RECORD_ID)
    if (
        lease.get("schemaVersion") != "aurora-deploy-file-editor-lease-v1"
        or lease.get("initialState") not in {"started", "stopped"}
        or not isinstance(lease.get("processId"), int)
        or isinstance(lease.get("processId"), bool)
        or lease["processId"] <= 0
        or not isinstance(lease.get("createdAt"), int)
        or lease.get("operationMode")
        not in {
            "preflight",
            "install",
            "status",
            "readback",
            "rollback",
            "recover-lock",
            "abort-stage",
            "finalize",
        }
        or (
            lease.get("transactionId") is not None
            and _validate_uuid(lease.get("transactionId")) != lease.get("transactionId")
        )
    ):
        raise BootstrapError("file_editor_lifecycle_lease_invalid")
    return lease


def _lifecycle_process_alive(process_id: int) -> bool:
    if process_id == os.getpid():
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_lifecycle_lease(
    operation_mode: str,
    transaction_id: str | None,
    *,
    create: bool,
    initial_state: str = "stopped",
) -> None:
    _write_local_record(
        LIFECYCLE_RECORD_ID,
        {
            "schemaVersion": "aurora-deploy-file-editor-lease-v1",
            "initialState": initial_state,
            "processId": os.getpid(),
            "createdAt": int(time.time()),
            "operationMode": operation_mode,
            "transactionId": transaction_id,
        },
        create=create,
    )


def _clear_lifecycle_lease() -> None:
    lease = _read_lifecycle_lease()
    if lease is None:
        return
    path = APPROVED_STATE_DIRECTORY / f"{LIFECYCLE_RECORD_ID}.json"
    path.unlink()
    directory_fd = os.open(APPROVED_STATE_DIRECTORY, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def home_assistant_config_check(url: str, token: str) -> None:
    request = urllib.request.Request(
        f"{url}/api/config/core/check_config",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=90) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception:
        raise BootstrapError("configuration_check_failed") from None
    result = _json_object(content, "configuration_check_failed")
    if result.get("result") != "valid" or result.get("errors") not in {None, ""}:
        raise BootstrapError("configuration_check_failed")


def verify_component_route(url: str, token: str, txid: str) -> None:
    request = urllib.request.Request(
        f"{url}/api/aurora/deploy-preview/v1/{txid}/readback",
        headers={"Authorization": f"Bearer {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        content = error.read(MAX_RESPONSE_BYTES + 1)
        if (
            error.code == 404
            and _json_object(content, "component_route_unverified").get("error_code")
            == "transaction_not_found"
        ):
            return
    except Exception:
        pass
    raise BootstrapError("component_route_unverified")


def _redacted_status(
    client: FileEditorIngressClient, txid: str, *, readback: bool = False
) -> dict[str, Any]:
    result = client.readback(txid) if readback else client.status(txid)
    allowed = {
        "status",
        "transactionId",
        "payloadManifestSha256",
        "configurationSha256",
        "componentTreeSha256",
        "trustSha256",
        "rollbackDeadline",
        "installerSha256",
        "replaceExact",
        "prestateConfigurationSha256",
        "prestateComponentTreeSha256",
        "prestateTrustSha256",
        "lockHeld",
        "lockOwnerMatches",
        "verified",
        "stagePresent",
        "lockCandidatePresent",
        "transactionPresent",
        "initializationPresent",
    }
    return {key: value for key, value in result.items() if key in allowed}


def _credential_transport_receipt(url: str) -> dict[str, str]:
    validated = _validate_url(url)
    return {
        "status": "credential_transport_ready",
        "credentialSource": "approved_root_env",
        "transport": "https"
        if urlparse(validated).scheme == "https"
        else "loopback_http",
    }


async def bootstrap(  # noqa: C901
    *,
    mode: str,
    source_root: Path,
    backup_id: str | None = None,
    expected_backup_agent_id: str | None = None,
    transaction_id: str | None = None,
    expected_configuration_sha256: str | None = None,
    expected_component_tree_sha256: str | None = None,
    expected_trust_sha256: str | None = None,
    expected_payload_manifest_sha256: str | None = None,
    expected_release_key_sha256: str | None = None,
    expected_validation_key_sha256: str | None = None,
    expected_source_revision: str | None = None,
    replace_exact: bool = False,
    socket_factory: Any = HomeAssistantSocket,
    ingress_client_factory: Any = FileEditorIngressClient,
    config_check: Any = home_assistant_config_check,
    component_route_check: Any = verify_component_route,
    credential_loader: Any = _load_credentials,
) -> dict[str, Any]:
    if mode not in {
        "credential-check",
        "preflight",
        "install",
        "status",
        "readback",
        "rollback",
        "recover-lock",
        "abort-stage",
        "finalize",
    }:
        raise BootstrapError("mode_invalid")
    url, token = credential_loader()
    if mode == "credential-check":
        return _credential_transport_receipt(url)
    txid = (
        None
        if mode == "preflight" and transaction_id is None
        else _validate_uuid(transaction_id)
    )
    payload = None
    if mode in {"preflight", "install"}:
        payload = validate_local_payload(
            source_root,
            expected_manifest_sha256=_validate_hash(
                expected_payload_manifest_sha256,
                "expected_payload_manifest_hash_required",
            ),
            expected_release_key_sha256=_validate_hash(
                expected_release_key_sha256, "expected_release_key_fingerprint_required"
            ),
            expected_validation_key_sha256=_validate_hash(
                expected_validation_key_sha256,
                "expected_validation_key_fingerprint_required",
            ),
            expected_source_revision=expected_source_revision or "",
        )
        config_check(url, token)
    recovery_arguments: dict[str, str | bool] = {}
    if mode == "install":
        expected_configuration_sha256 = _validate_hash(
            expected_configuration_sha256, "expected_configuration_hash_required"
        )
        expected_component_tree_sha256 = _validate_hash(
            expected_component_tree_sha256, "expected_component_tree_hash_required"
        )
        expected_trust_sha256 = _validate_hash(
            expected_trust_sha256, "expected_trust_hash_required", absent=True
        )
        _prepare_install_record(
            txid,
            {
                "schemaVersion": "aurora-deploy-bootstrap-local-v1",
                "status": "prepared",
                "transactionId": txid,
                "payloadManifestSha256": payload.manifest_sha256,
                "sourceRevision": payload.source_revision,
                "sourceRoot": str(source_root.resolve(strict=True)),  # noqa: ASYNC240
                "installerSha256": INSTALLER_SHA256,
                "installerSourceBase64": base64.b64encode(INSTALLER_SOURCE).decode(),
                "replaceExact": replace_exact,
                "expectedConfigurationSha256": expected_configuration_sha256,
                "expectedComponentTreeSha256": expected_component_tree_sha256,
                "expectedTrustSha256": expected_trust_sha256,
            },
        )
        recovery_arguments = {
            "expected-installer-sha256": INSTALLER_SHA256,
            "payload-manifest-sha256": payload.manifest_sha256,
            "expected-configuration-sha256": expected_configuration_sha256,
            "expected-component-tree-sha256": expected_component_tree_sha256,
            "expected-trust-sha256": expected_trust_sha256,
        }
    elif mode in {"rollback", "recover-lock", "abort-stage", "finalize"}:
        recovery_arguments = _persist_local_request(txid, mode)
    initial = None
    primary: BaseException | None = None
    result: dict[str, Any] | None = None
    try:
        initial, ingress, session = await _open_editor(
            url,
            token,
            backup_id,
            expected_backup_agent_id,
            mode in {"preflight", "install"},
            mode,
            txid,
            socket_factory,
        )
        client = ingress_client_factory(url, ingress, session)
        if mode in {"status", "readback"}:
            result = _bind_status_to_local(
                _redacted_status(client, txid, readback=mode == "readback"),
                txid,
                required=False,
            )
        elif mode == "preflight":
            result = {
                "status": "preflight_ready",
                "configurationSha256": _digest(client.configuration_bytes()),
                "componentTreeSha256": client.component_tree_sha256(),
                "trustSha256": client.trust_sha256(),
                "payloadManifestSha256": payload.manifest_sha256,
                "releaseKeySha256": payload.release_key_sha256,
                "validationKeySha256": payload.validation_key_sha256,
                "sourceRevision": payload.source_revision,
                "installerSha256": INSTALLER_SHA256,
            }
        elif mode == "install":
            config_check(url, token)
            if (
                _digest(client.configuration_bytes()) != expected_configuration_sha256
                or client.component_tree_sha256() != expected_component_tree_sha256
                or client.trust_sha256() != expected_trust_sha256
            ):
                raise BootstrapError("prestage_cas_mismatch")
            _stage_payload(client, txid, payload)
            result = client.execute(
                mode="install",
                transaction_id=txid,
                arguments={
                    "expected-configuration-sha256": expected_configuration_sha256,
                    "expected-component-tree-sha256": expected_component_tree_sha256,
                    "expected-trust-sha256": expected_trust_sha256,
                    "payload-manifest-sha256": payload.manifest_sha256,
                    "expected-installer-sha256": INSTALLER_SHA256,
                    **({"replace-exact": True} if replace_exact else {}),
                },
            )
            try:
                config_check(url, token)
                if (
                    _digest(client.configuration_bytes())
                    != result.get("configurationSha256")
                    or client.component_tree_sha256()
                    != result.get("componentTreeSha256")
                    or client.trust_sha256() != result.get("trustSha256")
                ):
                    raise BootstrapError("post_install_readback_mismatch")
            except BaseException:
                try:
                    rollback = client.execute(
                        mode="rollback",
                        transaction_id=txid,
                        arguments=recovery_arguments,
                    )
                    config_check(url, token)
                    if (
                        _digest(client.configuration_bytes())
                        != rollback.get("configurationSha256")
                        or client.component_tree_sha256()
                        != rollback.get("componentTreeSha256")
                        or client.trust_sha256() != rollback.get("trustSha256")
                    ):
                        raise BootstrapError("rollback_readback_mismatch")
                except BaseException:
                    raise BootstrapError(
                        "post_install_validation_failed_rollback_unverified"
                    ) from None
                raise BootstrapError(
                    "post_install_validation_failed_rolled_back"
                ) from None
            result["restartRequired"] = True
        elif mode == "rollback":
            result = client.execute(
                mode="rollback",
                transaction_id=txid,
                arguments=recovery_arguments,
            )
            config_check(url, token)
            if (
                _digest(client.configuration_bytes())
                != result.get("configurationSha256")
                or client.component_tree_sha256() != result.get("componentTreeSha256")
                or client.trust_sha256() != result.get("trustSha256")
            ):
                raise BootstrapError("rollback_readback_mismatch")
            result["restartRequired"] = True
        elif mode == "recover-lock":
            result = client.execute(
                mode="recover-lock",
                transaction_id=txid,
                arguments=recovery_arguments,
            )
        elif mode == "abort-stage":
            if client.transaction_exists(txid):
                raise BootstrapError("stage_abort_transaction_exists")
            _ensure_stage_abort_installer(
                client,
                txid,
                recovery_arguments["expected-installer-sha256"],
                _local_installer_bytes(_read_local_record(txid)),
            )
            result = client.execute(
                mode="abort-stage",
                transaction_id=txid,
                arguments={
                    "expected-installer-sha256": recovery_arguments[
                        "expected-installer-sha256"
                    ]
                },
            )
        else:
            status = _bind_status_to_local(
                _redacted_status(client, txid, readback=True), txid, required=True
            )
            local = _read_local_record(txid)
            if status.get("status") == "not_found":
                if (
                    local.get("status") not in {"finalize_authorized", "finalized"}
                    or status.get("lockHeld") is not False
                    or status.get("lockOwnerMatches") is not False
                    or status.get("stagePresent") is not False
                    or status.get("lockCandidatePresent") is not False
                    or status.get("transactionPresent") is not False
                    or status.get("initializationPresent") is not False
                ):
                    raise BootstrapError("finalize_not_allowed")
                expected_configuration = _validate_hash(
                    local.get("finalizeConfigurationSha256"),
                    "finalize_authorization_invalid",
                )
                expected_component = _validate_hash(
                    local.get("finalizeComponentTreeSha256"),
                    "finalize_authorization_invalid",
                )
                expected_trust = _validate_hash(
                    local.get("finalizeTrustSha256"),
                    "finalize_authorization_invalid",
                    absent=True,
                )
                config_check(url, token)
                if (
                    _digest(client.configuration_bytes()) != expected_configuration
                    or client.component_tree_sha256() != expected_component
                    or client.trust_sha256() != expected_trust
                ):
                    raise BootstrapError("finalize_readback_mismatch")
                result = {
                    "status": "finalized",
                    "transactionId": txid,
                    "payloadManifestSha256": _validate_hash(
                        local.get("payloadManifestSha256"),
                        "local_payload_pin_required",
                    ),
                    "reconciled": True,
                }
                status = None
            if status is None:
                pass
            elif status.get("status") not in {
                "installed",
                "restart_verified",
                "finalize_prepared",
                "rolled_back",
                "rolled_back_after_failure",
                "rolled_back_after_recovery",
                "rollback_finalize_prepared",
            }:
                raise BootstrapError("finalize_not_allowed")
            elif status.get("verified") is not True:
                raise BootstrapError("restart_readback_mismatch")
            else:
                config_check(url, token)
                if (
                    _digest(client.configuration_bytes())
                    != status.get("configurationSha256")
                    or client.component_tree_sha256()
                    != status.get("componentTreeSha256")
                    or client.trust_sha256() != status.get("trustSha256")
                ):
                    raise BootstrapError("restart_readback_mismatch")
            if status is not None and status.get("status") not in {
                "rolled_back",
                "rolled_back_after_failure",
                "rolled_back_after_recovery",
                "rollback_finalize_prepared",
            }:
                component_route_check(url, token, txid)
                if status.get("status") == "installed":
                    client.execute(
                        mode="mark-verified",
                        transaction_id=txid,
                        arguments=recovery_arguments,
                    )
            if status is not None:
                local["status"] = "finalize_authorized"
                local["finalizeConfigurationSha256"] = status["configurationSha256"]
                local["finalizeComponentTreeSha256"] = status["componentTreeSha256"]
                local["finalizeTrustSha256"] = status["trustSha256"]
                local["finalizeRemoteStatus"] = status["status"]
                _write_local_record(txid, local, create=False)
                result = client.execute(
                    mode="finalize",
                    transaction_id=txid,
                    arguments=recovery_arguments,
                )
    except BaseException as error:
        primary = error
    cleanup: BaseException | None = None
    if initial == "stopped":
        try:
            await _addon_action(url, token, "stop", socket_factory)
            _clear_lifecycle_lease()
        except BaseException as error:
            cleanup = error
    if primary is not None and cleanup is not None:
        raise BootstrapError(
            "operation_failed_and_file_editor_restore_failed"
        ) from None
    if cleanup is not None:
        raise BootstrapError("file_editor_restore_failed") from None
    if primary is not None:
        if isinstance(primary, (BootstrapError, asyncio.CancelledError)):
            raise primary
        raise BootstrapError("operation_failed") from None
    if result is None:
        raise BootstrapError("operation_failed")
    if txid is not None and mode in {
        "install",
        "rollback",
        "recover-lock",
        "abort-stage",
        "finalize",
    }:
        local = _read_local_record(txid)
        local["status"] = str(result.get("status"))
        local["receiptSha256"] = _digest(_canonical(result))
        _write_local_record(txid, local, create=False)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in (
        "credential-check",
        "preflight",
        "install",
        "status",
        "readback",
        "rollback",
        "recover-lock",
        "abort-stage",
        "finalize",
    ):
        modes.add_argument(f"--{name}", action="store_true")
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--backup-id")
    parser.add_argument("--expected-backup-agent-id")
    parser.add_argument("--transaction-id")
    parser.add_argument("--expected-configuration-sha256")
    parser.add_argument("--expected-component-tree-sha256")
    parser.add_argument("--expected-trust-sha256")
    parser.add_argument("--expected-payload-manifest-sha256")
    parser.add_argument("--expected-release-key-sha256")
    parser.add_argument("--expected-validation-key-sha256")
    parser.add_argument("--expected-source-revision")
    parser.add_argument("--replace-exact", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = next(
        name
        for name in (
            "credential_check",
            "preflight",
            "install",
            "status",
            "readback",
            "rollback",
            "recover_lock",
            "abort_stage",
            "finalize",
        )
        if getattr(args, name)
    )
    mode = mode.replace("_", "-")
    try:
        result = asyncio.run(
            bootstrap(
                mode=mode,
                source_root=args.source_root,
                backup_id=args.backup_id,
                expected_backup_agent_id=args.expected_backup_agent_id,
                transaction_id=args.transaction_id,
                expected_configuration_sha256=args.expected_configuration_sha256,
                expected_component_tree_sha256=args.expected_component_tree_sha256,
                expected_trust_sha256=args.expected_trust_sha256,
                expected_payload_manifest_sha256=args.expected_payload_manifest_sha256,
                expected_release_key_sha256=args.expected_release_key_sha256,
                expected_validation_key_sha256=args.expected_validation_key_sha256,
                expected_source_revision=args.expected_source_revision,
                replace_exact=args.replace_exact,
            )
        )
    except BootstrapError as error:
        print(_canonical({"status": "failed", "error": str(error)}).decode())
        return 1
    except Exception:
        print(_canonical({"status": "failed", "error": "operation_failed"}).decode())
        return 1
    print(_canonical(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
