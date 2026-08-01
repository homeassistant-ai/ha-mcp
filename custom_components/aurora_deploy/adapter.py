"""Narrow Aurora deployment API.

Only the fixed preview and production pointers are addressable. This module has
no path, shell, upload, Supervisor, File Editor, SSH, or arbitrary Lovelace API.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import tarfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover - standalone validation tests
    class _WebStub:
        class Request:
            pass

        class Response:
            pass

    web = _WebStub()  # type: ignore[assignment]

try:  # Keep pure validation helpers testable in the ha-mcp-only environment.
    from homeassistant.components.http import HomeAssistantView
    from homeassistant.components.lovelace.const import (
        CONF_ICON,
        CONF_REQUIRE_ADMIN,
        CONF_SHOW_IN_SIDEBAR,
        CONF_TITLE,
        CONF_URL_PATH,
    )
    from homeassistant.core import HomeAssistant
except ModuleNotFoundError:  # pragma: no cover - only used by standalone unit tests
    class HomeAssistantView:  # type: ignore[no-redef]
        requires_auth = True

    HomeAssistant = Any  # type: ignore[misc,assignment]
    CONF_ICON = "icon"
    CONF_REQUIRE_ADMIN = "require_admin"
    CONF_SHOW_IN_SIDEBAR = "show_in_sidebar"
    CONF_TITLE = "title"
    CONF_URL_PATH = "url_path"

DOMAIN = "aurora_deploy"
BASE = "/api/aurora/deploy-preview/v1"
PREVIEW = "home-command-preview"
PRODUCTION = "home-command"
# A prior Aurora release used this target. It is a fixed migration collision, not a caller-
# selectable dashboard id; refusing it prevents bootstrap from creating a third Aurora environment.
LEGACY_PREVIEW = "aurora-preview"
TARGET = "aurora-v9-preview"
APPROVED_RELEASE = "0.1.16"
MAX_BODY = 80 * 1024 * 1024
MAX_MANIFEST = 512 * 1024
MAX_PACKAGE = 64 * 1024 * 1024
MAX_DASHBOARD = 8 * 1024 * 1024
MAX_MEMBERS = 256
MAX_EXPANDED = 32 * 1024 * 1024
CLOCK_SKEW = timedelta(minutes=5)
MAX_LIFETIME = timedelta(hours=24)
MAX_REQUESTS_PER_MINUTE = 60
REQUEST_WINDOW_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 30.0
_SAFE_NONCE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
PACKAGE_ROOT = "custom_components/aurora_camera_ai/"
PACKAGE_ROOT_FILES = frozenset(
    {
        "manifest.json",
        "activation-manifest.json",
        "custom-component-manifest.json",
        "install-aurora-camera-ai-component.py",
    }
)
COMPONENT_DOMAIN = "aurora_camera_ai"
COMPONENT_VERSION = "0.3.0"
APPROVED_COMPONENT_FILES = frozenset(
    {
        "__init__.py",
        "analysis_models.py",
        "api.py",
        "attention_policy.py",
        "coordinator.py",
        "event_store.py",
        "manifest.json",
        "models.py",
        "review_store.py",
        "services.yaml",
        "timeline_contract.py",
        "vehicle_catalog_v1.json",
    }
)
APPROVED_PACKAGE_FILES = PACKAGE_ROOT_FILES | frozenset(
    PACKAGE_ROOT + filename for filename in APPROVED_COMPONENT_FILES
)
APPROVED_PACKAGE_DIRECTORIES = frozenset(
    {"custom_components", "custom_components/aurora_camera_ai"}
)
VALIDATION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "preview_revision",
        "expected_production_revision",
        "preview_config_sha256",
        "expected_production_config_sha256",
        "dashboard_target",
        "physical_validation",
        "device_results",
        "manifest_sha256",
        "package_sha256",
        "dashboard_sha256",
        "issued_at",
        "nonce",
        "signer",
        "signature",
        "expires_at",
    }
)
DASHBOARD_URL = "/local/aurora/aurora-preview-dashboard.js"
DASHBOARD_URL_PREFIX = "/local/aurora/revisions/"
PRIVACY_POLICY = "no-sensitive-inference-v1"
FORBIDDEN_PRIVACY = frozenset(
    {
        "biometric",
        "face recognition",
        "facial recognition",
        "identity inference",
        "appearance inference",
        "appearance profiling",
        "license plate",
        "licence plate",
        "plate recognition",
        "intent inference",
        "emotion inference",
        "criminality",
        "suspicion score",
    }
)
_PROHIBITION = re.compile(
    r"\b(?:never|no|not|without|do\s+not|don't|must\s+not|prohibit(?:s|ed)?|"
    r"forbid(?:s|den)?|deny[- ]?list|reject(?:s|ed|ing)?|refus(?:e|es|ed|ing)|"
    r"removed|disallow(?:s|ed)?|forbidden)\b",
    re.IGNORECASE,
)
_CAPABILITY = re.compile(
    r"\b(?:"
    r"biometric[_\s-]?(?:template|templates|embedding|embeddings|match|matches|id)?|"
    r"(?:face|facial)[_\s-]?(?:match|matching|recognition|recognise|recognize|embedding|template|id)|"
    r"identity[_\s-]?(?:match|matching|result|results|inference|id|embedding)|"
    r"(?:appearance)[_\s-]?(?:profile|profiling|inference|result)|"
    r"(?:license|licence|number)[_\s-]?plate[_\s-]?(?:ocr|text|number|read|reading|extract|extraction|result)?|"
    r"plate[_\s-]?(?:ocr|text|number|read|reading|extract|extraction|recognition|result)|"
    r"ocr[_\s-]?(?:text|plate|number|read|reading|extract|extraction|result)|"
    r"(?:emotion|gender|ethnicity|racial?|age|criminality|intent|suspicion)[_\s-]?"
    r"(?:score|scores|label|labels|inference|inferred|result|results|classification|class)"
    r")\b",
    re.IGNORECASE | re.VERBOSE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?m)(?:^|[,\[{(]\s*)['\"]?"
    r"(?:biometric|face|facial|identity|appearance|license|licence|plate|ocr|"
    r"emotion|gender|ethnicity|race|age|criminality|intent|suspicion)"
    r"[A-Za-z0-9_-]*['\"]?\s*[:=]",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _config_sha256(config: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(config)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _error(status: int, code: str) -> web.Response:
    return _json_response({"error_code": code}, status)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp")
    return result.astimezone(UTC)


def _canonical_manifest(manifest: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return _json_bytes(unsigned)


def _decode_b64(value: Any, maximum: int) -> bytes:
    if not isinstance(value, str) or len(value) > maximum * 2:
        raise ValueError("encoding")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("encoding") from exc
    if len(raw) > maximum:
        raise ValueError("size")
    return raw


def _trusted_keys(hass: HomeAssistant) -> dict[str, bytes]:
    """Load pinned public keys from a fixed, operator-managed file."""
    path = Path(hass.config.path("aurora_deploy_trusted_keys.json"))
    try:
        if not path.is_file() or path.is_symlink():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, bytes] = {}
    for key_id, encoded in data.items():
        if not isinstance(key_id, str) or not isinstance(encoded, str):
            continue
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            continue
        if len(key) == 32 and key_id and len(key_id) <= 64:
            result[key_id] = key
    return result


def _verify_signature(hass: HomeAssistant, document: dict[str, Any], *, prefix: str) -> None:
    key_id = document.get("key_id") or document.get("signer")
    signature_text = document.get("signature")
    if not isinstance(key_id, str) or not isinstance(signature_text, str):
        raise ValueError("signature")
    if prefix and not key_id.startswith(prefix):
        raise ValueError("signer")
    try:
        signature = base64.b64decode(signature_text, validate=True)
        public = _trusted_keys(hass)[key_id]
        if len(signature) != 64:
            raise ValueError("signature")
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(public).verify(signature, _canonical_manifest(document))
        except InvalidSignature as exc:
            raise ValueError("signature") from exc
    except (KeyError, ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("signature") from exc


def _privacy_guard_context(text: str, line_number: int) -> bool:
    lines = text.splitlines()
    start = max(0, line_number - 4)
    end = min(len(lines), line_number + 5)
    context = "\n".join(lines[start:end])
    return bool(_PROHIBITION.search(context)) or "denylist" in context.casefold()


def _scan_privacy(raw: bytes) -> None:
    text = raw.decode("utf-8", errors="ignore")
    lowered = text.casefold()
    for term in FORBIDDEN_PRIVACY:
        start = 0
        needle = term.casefold()
        while (offset := lowered.find(needle, start)) != -1:
            if not _privacy_guard_context(text, text.count("\n", 0, offset)):
                raise ValueError("privacy")
            start = offset + len(needle)
    for match in _CAPABILITY.finditer(text):
        line = text.count("\n", 0, match.start())
        line_text = text.splitlines()[line]
        executable_shape = bool(
            re.search(r"\b[A-Za-z_][A-Za-z0-9_-]*['\"]?\s*[:=]", line_text)
        )
        if not _privacy_guard_context(text, line) or executable_shape:
            raise ValueError("privacy")
    for match in _SENSITIVE_ASSIGNMENT.finditer(text):
        line = text.count("\n", 0, match.start())
        if not _privacy_guard_context(text, line):
            raise ValueError("privacy")


def _json_object(raw: bytes, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(error_code) from exc
    if not isinstance(value, dict):
        raise ValueError(error_code)
    return value


def _package_members(raw: bytes) -> dict[str, bytes]:  # noqa: C901 - archive policy is intentionally explicit
    if not raw.startswith(b"\x1f\x8b"):
        raise ValueError("package_format")
    count = 0
    expanded = 0
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                count += 1
                if count > MAX_MEMBERS:
                    raise ValueError("package_members")
                name = member.name.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or name == "":
                    raise ValueError("package_path")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValueError("package_link")
                normalized = name.rstrip("/")
                if member.isdir():
                    if normalized not in APPROVED_PACKAGE_DIRECTORIES:
                        raise ValueError("package_member")
                    continue
                if not member.isfile():
                    raise ValueError("package_member_type")
                if name not in APPROVED_PACKAGE_FILES:
                    raise ValueError("package_member")
                if name.endswith((".tar", ".tar.gz", ".tgz", ".zip")):
                    raise ValueError("nested_archive")
                if name in files:
                    raise ValueError("package_duplicate")
                expanded += max(0, member.size)
                if expanded > MAX_EXPANDED:
                    raise ValueError("package_expanded")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("package_member")
                data = stream.read(member.size + 1)
                if len(data) != member.size:
                    raise ValueError("package_size")
                _scan_privacy(data)
                files[name] = data
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("package_format") from exc

    if set(files) != APPROVED_PACKAGE_FILES:
        raise ValueError("package_source_missing")

    component_manifest_raw = files["custom-component-manifest.json"]
    component_manifest = _json_object(
        component_manifest_raw, "package_component_manifest"
    )
    if any(
        (
            component_manifest.get("schemaVersion") != "1.0",
            component_manifest.get("domain") != COMPONENT_DOMAIN,
            component_manifest.get("version") != COMPONENT_VERSION,
            component_manifest.get("configurationKey") != COMPONENT_DOMAIN,
            component_manifest.get("restartRequired") is not True,
        )
    ):
        raise ValueError("package_component_manifest")
    installation = component_manifest.get("installation")
    rollback = component_manifest.get("rollback")
    if not isinstance(installation, dict) or any(
        (
            installation.get("mode") != "transactional-atomic-rename",
            installation.get("installer")
            != "install-aurora-camera-ai-component.py",
            installation.get("configurationHashGuardRequired") is not True,
            installation.get("prestateCapture") != "exact-bytes",
        )
    ):
        raise ValueError("package_component_manifest")
    if not isinstance(rollback, dict) or any(
        (
            rollback.get("mode") != "restore-exact-prestate",
            rollback.get("configurationHashGuardRequired") is not True,
            rollback.get("restartRequired") is not True,
        )
    ):
        raise ValueError("package_component_manifest")

    entries = component_manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("package_component_manifest")
    described: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("package_component_manifest")
        path = entry.get("path")
        if not isinstance(path, str) or path not in APPROVED_PACKAGE_FILES:
            raise ValueError("package_component_manifest")
        if not path.startswith(PACKAGE_ROOT) or path in described:
            raise ValueError("package_component_manifest")
        data = files[path]
        if entry.get("size") != len(data) or entry.get("sha256") != hashlib.sha256(
            data
        ).hexdigest():
            raise ValueError("package_component_hash")
        described.add(path)
    expected_component_paths = {
        PACKAGE_ROOT + filename for filename in APPROVED_COMPONENT_FILES
    }
    if described != expected_component_paths:
        raise ValueError("package_source_missing")

    package_manifest = _json_object(files["manifest.json"], "package_manifest")
    installer = package_manifest.get("installer")
    if any(
        (
            package_manifest.get("entry") != "activation-manifest.json",
            package_manifest.get("sha256")
            != hashlib.sha256(files["activation-manifest.json"]).hexdigest(),
            package_manifest.get("componentManifestEntry")
            != "custom-component-manifest.json",
            package_manifest.get("componentManifestSha256")
            != hashlib.sha256(component_manifest_raw).hexdigest(),
            not isinstance(installer, dict),
        )
    ):
        raise ValueError("package_manifest")
    installer_bytes = files["install-aurora-camera-ai-component.py"]
    if not isinstance(installer, dict) or any(
        (
            installer.get("entry") != "install-aurora-camera-ai-component.py",
            installer.get("size") != len(installer_bytes),
            installer.get("sha256")
            != hashlib.sha256(installer_bytes).hexdigest(),
        )
    ):
        raise ValueError("package_manifest")

    integration_manifest = _json_object(
        files[PACKAGE_ROOT + "manifest.json"], "package_integration_manifest"
    )
    if (
        integration_manifest.get("domain") != COMPONENT_DOMAIN
        or integration_manifest.get("version") != COMPONENT_VERSION
    ):
        raise ValueError("package_integration_manifest")
    return {
        filename: files[PACKAGE_ROOT + filename]
        for filename in APPROVED_COMPONENT_FILES
    }


def _validate_package(raw: bytes) -> dict[str, bytes]:
    """Return the exact reviewed component payload after fail-closed validation."""
    return _package_members(raw)


def _component_binding(members: dict[str, bytes]) -> str:
    if set(members) != APPROVED_COMPONENT_FILES:
        raise ValueError("package_source_missing")
    digest = hashlib.sha256()
    for filename in sorted(APPROVED_COMPONENT_FILES):
        data = members[filename]
        if not isinstance(data, bytes):
            raise ValueError("package_member")
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _live_component_members(hass: HomeAssistant) -> dict[str, bytes]:
    root = Path(hass.config.path(f"custom_components/{COMPONENT_DOMAIN}"))
    if root.is_symlink() or not root.is_dir():
        raise ValueError("active_component_missing")
    members: dict[str, bytes] = {}
    for filename in APPROVED_COMPONENT_FILES:
        path = root / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("active_component_missing")
        members[filename] = path.read_bytes()
    return members


def _transaction_token(transaction: dict[str, Any]) -> str:
    transaction_id = transaction.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise ValueError("transaction_id")
    return hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()


def _activate_component_package(
    hass: HomeAssistant,
    state: "AuroraState",
    transaction: dict[str, Any],
    members: dict[str, bytes],
) -> str:
    """Atomically replace only the fixed Aurora component directory."""
    binding = _component_binding(members)
    token = _transaction_token(transaction)
    attempt = transaction.get("component_activation_attempt", 0)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise ValueError("component_activation_attempt")
    attempt += 1
    transaction["component_activation_attempt"] = attempt
    attempt_name = str(attempt)
    candidate_root = state.root / "component-candidates" / token / attempt_name
    candidate = candidate_root / COMPONENT_DOMAIN
    prestate = state.root / "component-prestate" / token / attempt_name
    if candidate_root.exists() or prestate.exists():
        raise ValueError("component_transaction_collision")
    candidate.mkdir(parents=True)
    for filename, data in members.items():
        _atomic_write(candidate / filename, data)

    parent = Path(hass.config.path("custom_components"))
    if parent.is_symlink():
        raise ValueError("component_parent")
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / COMPONENT_DOMAIN
    if destination.is_symlink():
        raise ValueError("component_destination")
    previous_exists = destination.exists()
    if previous_exists and not destination.is_dir():
        raise ValueError("component_destination")
    moved_previous = False
    try:
        if previous_exists:
            prestate.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, prestate)
            moved_previous = True
        os.replace(candidate, destination)
        candidate_root.rmdir()
    except OSError:
        if moved_previous and not destination.exists() and prestate.exists():
            os.replace(prestate, destination)
        raise
    transaction["component_prestate_exists"] = previous_exists
    transaction["component_binding_sha256"] = binding
    return binding


def _restore_component_prestate(
    hass: HomeAssistant, state: "AuroraState", transaction: dict[str, Any]
) -> None:
    """Restore the exact component directory captured by this transaction."""
    if "component_prestate_exists" not in transaction:
        return
    expected = transaction.get("component_binding_sha256")
    if not isinstance(expected, str):
        raise ValueError("component_rollback_snapshot_missing")
    live = _component_binding(_live_component_members(hass))
    if live != expected:
        raise ValueError("component_cas_mismatch")
    token = _transaction_token(transaction)
    attempt = transaction.get("component_activation_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("component_rollback_snapshot_missing")
    attempt_name = str(attempt)
    destination = Path(hass.config.path(f"custom_components/{COMPONENT_DOMAIN}"))
    prestate = state.root / "component-prestate" / token / attempt_name
    retired = state.root / "component-retired" / token / attempt_name
    if retired.exists():
        raise ValueError("component_transaction_collision")
    retired.parent.mkdir(parents=True, exist_ok=True)
    os.replace(destination, retired)
    if transaction["component_prestate_exists"]:
        if prestate.is_symlink() or not prestate.is_dir():
            os.replace(retired, destination)
            raise ValueError("component_rollback_snapshot_missing")
        try:
            os.replace(prestate, destination)
        except OSError:
            os.replace(retired, destination)
            raise


async def _verify_active_bindings(
    hass: HomeAssistant,
    transaction: dict[str, Any],
    members: dict[str, bytes],
    dashboard_bytes: bytes,
) -> str:
    expected_component = _component_binding(members)
    if transaction.get("component_binding_sha256") != expected_component:
        raise ValueError("active_component_binding")
    live_members = await asyncio.to_thread(_live_component_members, hass)
    if _component_binding(live_members) != expected_component:
        raise ValueError("active_component_binding")

    dashboard_sha = hashlib.sha256(dashboard_bytes).hexdigest()
    asset_name = f"aurora-preview-dashboard-{dashboard_sha}.js"
    if transaction.get("active_dashboard_asset") != asset_name:
        raise ValueError("active_dashboard_binding")
    asset_path = Path(hass.config.path(f"www/aurora/revisions/{asset_name}"))
    if asset_path.is_symlink() or not asset_path.is_file():
        raise ValueError("active_dashboard_binding")
    if hashlib.sha256(asset_path.read_bytes()).hexdigest() != dashboard_sha:
        raise ValueError("active_dashboard_binding")
    _dashboard, config = await _load_dashboard(hass, PREVIEW)
    resources = config.get("resources")
    expected_url = f"{DASHBOARD_URL_PREFIX}{asset_name}"
    if not isinstance(resources, list) or not any(
        isinstance(item, dict)
        and item.get("url") == expected_url
        and item.get("res_type") == "module"
        for item in resources
    ):
        raise ValueError("active_dashboard_binding")
    return expected_component


async def _verify_active_transaction(
    hass: HomeAssistant, transaction: dict[str, Any], root: Path | None = None
) -> str:
    package, dashboard, _manifest = _verify_staged_artifacts(transaction, root)
    if package is None or dashboard is None:
        raise ValueError("staged_artifact_missing")
    members = _validate_package(package)
    return await _verify_active_bindings(hass, transaction, members, dashboard)


def _validate_manifest(hass: HomeAssistant, manifest: Any, package: bytes, dashboard: bytes) -> tuple[str, str, str]:  # noqa: C901 - fail-closed policy checks are kept together
    if not isinstance(manifest, dict) or len(_json_bytes(manifest)) > MAX_MANIFEST:
        raise ValueError("manifest")
    if manifest.get("schema_version") != 1 or manifest.get("target") != TARGET:
        raise ValueError("target")
    if manifest.get("dashboard_target", PREVIEW) != PREVIEW or manifest.get("preview_only") is not True:
        raise ValueError("dashboard")
    if manifest.get("target_release") != APPROVED_RELEASE:
        raise ValueError("release")
    if manifest.get("privacy_policy") != "no-sensitive-inference-v1":
        raise ValueError("privacy_policy")
    issued = _parse_time(manifest.get("issued_at", manifest.get("created_at")))
    expires = _parse_time(manifest.get("expires_at"))
    now = _now()
    if issued - CLOCK_SKEW > now or expires + CLOCK_SKEW < now or expires <= issued or expires - issued > MAX_LIFETIME:
        raise ValueError("expiry")
    nonce = manifest.get("nonce")
    if not isinstance(nonce, str) or not (8 <= len(nonce) <= 128) or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in nonce):
        raise ValueError("nonce")
    _verify_signature(hass, manifest, prefix="release-")
    package_hash = hashlib.sha256(package).hexdigest()
    dashboard_hash = hashlib.sha256(dashboard).hexdigest()
    if manifest.get("artifact_sha256", manifest.get("package_sha256")) != package_hash or manifest.get("dashboard_sha256") != dashboard_hash:
        raise ValueError("hash")
    assets = manifest.get("assets")
    expected = {"aurora-preview-package": package_hash, "aurora-preview-dashboard": dashboard_hash}
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("assets")
    seen: set[str] = set()
    for item in assets:
        if not isinstance(item, dict) or item.get("name") not in expected or item["name"] in seen or item.get("sha256") != expected[item["name"]]:
            raise ValueError("assets")
        seen.add(item["name"])
    if seen != set(expected):
        raise ValueError("assets")
    _scan_privacy(_json_bytes(manifest))
    _scan_privacy(dashboard)
    _validate_package(package)
    return hashlib.sha256(_canonical_manifest(manifest)).hexdigest(), package_hash, dashboard_hash


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _staged_revision_dir(transaction: dict[str, Any], root: Path) -> Path:
    revision = transaction.get("revision")
    if not isinstance(revision, str) or not _SAFE_REVISION.fullmatch(revision):
        raise ValueError("staged_path_confined")
    if revision in {".", ".."} or root.is_symlink():
        raise ValueError("staged_path_confined")
    staged_root = root / "staged"
    if staged_root.is_symlink() or not staged_root.is_dir():
        raise ValueError("staged_path_confined")
    try:
        if staged_root.resolve(strict=True).parent != root.resolve(strict=True):
            raise ValueError("staged_path_confined")
    except OSError as exc:
        raise ValueError("staged_path_confined") from exc
    candidate = staged_root / revision
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
        raise ValueError("staged_path_confined")
    return candidate


def _read_staged_file(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("staged_artifact_invalid") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("staged_artifact_invalid")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = -1
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("staged_artifact_oversized")
        return data
    except OSError as exc:
        raise ValueError("staged_artifact_invalid") from exc
    finally:
        if fd != -1:
            os.close(fd)


def _verify_staged_artifacts(
    transaction: dict[str, Any],
    root: Path,
) -> tuple[bytes | None, bytes | None, dict[str, Any] | None]:
    """Read and rehash all staged artifacts beneath the fixed state root."""
    revision_dir = _staged_revision_dir(transaction, root)
    manifest_path = revision_dir / "manifest.json"
    package_path = revision_dir / "aurora-preview-package.tar.gz"
    dashboard_path = revision_dir / "aurora-preview-dashboard.js"
    expected = {
        "manifest_sha256": transaction.get("manifest_sha256"),
        "package_sha256": transaction.get("package_sha256"),
        "dashboard_sha256": transaction.get("dashboard_sha256"),
    }
    paths = (manifest_path, package_path, dashboard_path)
    for path in paths:
        if path.is_symlink():
            raise ValueError("staged_artifact_symlink")
        if not path.exists():
            # Transactions created before staged readback verification did not retain
            # a complete artifact set. New transactions always have all three hashes.
            if all(isinstance(value, str) for value in expected.values()):
                raise ValueError("staged_artifact_missing")
            return None, None, None
        if not path.is_file():
            raise ValueError("staged_artifact_invalid")
    try:
        manifest = json.loads(
            _read_staged_file(manifest_path, MAX_MANIFEST).decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("staged_manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("staged_manifest_invalid")
    package = _read_staged_file(package_path, MAX_PACKAGE)
    dashboard = _read_staged_file(dashboard_path, MAX_DASHBOARD)
    actual = {
        "manifest_sha256": hashlib.sha256(_canonical_manifest(manifest)).hexdigest(),
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "dashboard_sha256": hashlib.sha256(dashboard).hexdigest(),
    }
    if any(
        isinstance(expected_hash, str) and actual[name] != expected_hash
        for name, expected_hash in expected.items()
    ):
        raise ValueError("staged_artifact_hash_mismatch")
    if all(isinstance(value, str) for value in expected.values()):
        calculated_revision = hashlib.sha256(
            (
                actual["manifest_sha256"]
                + actual["package_sha256"]
                + actual["dashboard_sha256"]
            ).encode()
        ).hexdigest()[:32]
        if calculated_revision != transaction.get("revision"):
            raise ValueError("staged_revision_mismatch")
    return package, dashboard, manifest


def _save_immutable_asset(path: Path, data: bytes) -> None:
    """Create an immutable content-addressed asset or verify its existing bytes."""
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ValueError("dashboard_asset_collision")
        return
    _atomic_write(path, data)


@dataclass
class AuroraState:
    hass: HomeAssistant
    root: Path
    journal: dict[str, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=__import__("asyncio").Lock)
    request_times: deque[float] = field(default_factory=deque)

    @classmethod
    async def create(cls, hass: HomeAssistant) -> AuroraState:
        return await hass.async_add_executor_job(cls._create_sync, hass)

    @classmethod
    def _create_sync(cls, hass: HomeAssistant) -> AuroraState:
        root = Path(hass.config.path(".storage/aurora_deploy_preview"))
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        journal_path = root / "journal.json"
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("aurora_deploy_journal_corrupt") from exc
        if not isinstance(journal, dict):
            raise RuntimeError("aurora_deploy_journal_invalid")
        transition = journal.get("production_transition")
        if isinstance(transition, dict) and transition.get("status") == "prepared":
            journal["production_recovery_required"] = True
        return cls(hass, root, journal)

    def save(self) -> None:
        _atomic_write(self.root / "journal.json", _json_bytes(self.journal))

    def tx(self, transaction_id: str) -> dict[str, Any] | None:
        value = self.journal.get("transactions", {}).get(transaction_id)
        return value if isinstance(value, dict) else None

    def admit_request(self) -> bool:
        now = time.monotonic()
        while self.request_times and now - self.request_times[0] >= REQUEST_WINDOW_SECONDS:
            self.request_times.popleft()
        if len(self.request_times) >= MAX_REQUESTS_PER_MINUTE:
            return False
        self.request_times.append(now)
        return True


def _ensure_production_baseline(state: AuroraState, config_sha256: str) -> str:
    """Persist a deterministic CAS revision for a fresh installation."""
    current = state.journal.get("production_revision")
    recorded_hash = state.journal.get("production_config_sha256")
    if isinstance(current, str) and current:
        if _is_sha256(recorded_hash) and recorded_hash != config_sha256:
            raise ValueError("production_config_conflict")
        if recorded_hash != config_sha256:
            state.journal["production_config_sha256"] = config_sha256
        return current
    baseline = "baseline-" + config_sha256
    state.journal["production_revision"] = baseline
    state.journal["production_config_sha256"] = config_sha256
    state.save()
    return baseline


async def _admin(request: web.Request) -> bool:
    user = request.get("hass_user")
    return bool(user is not None and getattr(user, "is_admin", False))


async def _body(request: web.Request) -> dict[str, Any] | None:
    if request.content_length and request.content_length > MAX_BODY:
        return None
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return None
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


async def _dashboards(hass: HomeAssistant) -> tuple[Any, dict[Any, Any]]:
    from homeassistant.components.lovelace import LOVELACE_DATA
    data = hass.data[LOVELACE_DATA]
    return data, data.dashboards


async def _ensure_preview(hass: HomeAssistant) -> tuple[bool, str]:
    data, dashboards = await _dashboards(hass)
    if LEGACY_PREVIEW in dashboards:
        raise ValueError("legacy_preview_collision")
    existing = dashboards.get(PREVIEW)
    metadata = {
        CONF_URL_PATH: PREVIEW,
        CONF_TITLE: "Aurora Preview",
        CONF_ICON: "mdi:aurora",
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_REQUIRE_ADMIN: True,
    }
    if existing is not None:
        config = getattr(existing, "config", {}) or {}
        if any(config.get(key) != value for key, value in metadata.items()):
            raise ValueError("preview_collision")
        return False, PREVIEW
    from homeassistant.components.lovelace import _register_panel, dashboard

    collection = dashboard.DashboardsCollection(hass)
    await collection.async_load()
    item = await collection.async_create_item(dict(metadata))
    dashboards[PREVIEW] = dashboard.LovelaceStorage(hass, item)
    _register_panel(hass, PREVIEW, dashboard.MODE_STORAGE, item, False)
    return True, PREVIEW


async def _load_dashboard(hass: HomeAssistant, url_path: str) -> tuple[Any, dict[str, Any]]:
    _data, dashboards = await _dashboards(hass)
    dashboard = dashboards.get(url_path)
    if dashboard is None:
        raise ValueError("dashboard_missing")
    config = await dashboard.async_load(False)
    return dashboard, config if isinstance(config, dict) else {"views": []}


async def _save_preview_asset(hass: HomeAssistant, dashboard_bytes: bytes) -> str:
    asset_revision = hashlib.sha256(dashboard_bytes).hexdigest()
    asset_name = f"aurora-preview-dashboard-{asset_revision}.js"
    path = Path(hass.config.path(f"www/aurora/revisions/{asset_name}"))
    await asyncio.to_thread(_save_immutable_asset, path, dashboard_bytes)
    dashboard_url = f"{DASHBOARD_URL_PREFIX}{asset_name}"
    dashboard, config = await _load_dashboard(hass, PREVIEW)
    resources = config.get("resources")
    if not isinstance(resources, list):
        resources = []
    resources = [
        item
        for item in resources
        if not (
            isinstance(item, dict)
            and (
                item.get("url") == DASHBOARD_URL
                or (
                    isinstance(item.get("url"), str)
                    and item["url"].startswith(DASHBOARD_URL_PREFIX)
                )
            )
        )
    ]
    resources.append({"url": dashboard_url, "res_type": "module"})
    config["resources"] = resources
    await dashboard.async_save(config)
    return asset_name


async def _reload_backend(hass: HomeAssistant) -> None:
    """Reload only the fixed Lovelace resources and core configuration hooks."""
    services = getattr(hass, "services", None)
    has_service = getattr(services, "has_service", None)
    async_call = getattr(services, "async_call", None)
    if not callable(has_service) or not callable(async_call):
        raise RuntimeError("reload_backend_unavailable")
    called = False
    for domain, service in (("lovelace", "reload_resources"), ("homeassistant", "reload_core_config")):
        if has_service(domain, service):
            await async_call(domain, service, {}, blocking=True)
            called = True
    if not called:
        raise RuntimeError("reload_backend_unavailable")


async def _reconcile_production_transition(
    hass: HomeAssistant, state: AuroraState
) -> None:
    """Resolve one durable prepared promotion from live production bytes."""
    transition = state.journal.get("production_transition")
    if not isinstance(transition, dict) or transition.get("status") != "prepared":
        state.journal.pop("production_recovery_required", None)
        return
    previous = transition.get("previous")
    next_config = transition.get("next_config")
    transaction_id = transition.get("transaction_id")
    receipt_nonce = transition.get("receipt_nonce")
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("config"), dict)
        or not _is_sha256(previous.get("config_sha256"))
        or not isinstance(next_config, dict)
        or not _is_sha256(transition.get("next_config_sha256"))
        or not isinstance(transaction_id, str)
        or not isinstance(receipt_nonce, str)
    ):
        raise ValueError("production_transition_recovery_invalid")
    transaction = state.tx(transaction_id)
    if transaction is None or transaction.get("revision") != transition.get(
        "to_revision"
    ):
        raise ValueError("production_transition_recovery_invalid")
    _production, current_config = await _load_dashboard(hass, PRODUCTION)
    current_sha = _config_sha256(current_config)
    previous_sha = previous["config_sha256"]
    next_sha = transition["next_config_sha256"]
    if current_sha == next_sha:
        committed_at = _now().isoformat()
        state.journal["previous_production"] = previous
        state.journal["production_revision"] = transition["to_revision"]
        state.journal["production_config_sha256"] = next_sha
        state.journal.setdefault("receipt_nonces", {})[receipt_nonce] = transaction_id
        transaction["status"] = "promoted"
        transaction["promoted_at"] = committed_at
        transition["status"] = "committed"
        transition["committed_at"] = committed_at
        transition["recovered"] = True
    elif current_sha == previous_sha:
        transition["status"] = "aborted"
        transition["aborted_at"] = _now().isoformat()
        transition["recovered"] = True
    else:
        raise ValueError("production_transition_recovery_conflict")
    state.journal.pop("production_recovery_required", None)
    state.save()


class RootView(HomeAssistantView):
    """Bootstrap, stage, promote, and rollback routes."""

    requires_auth = True
    url = BASE + "/{operation}"
    name = DOMAIN + ":root"

    def __init__(self, hass: HomeAssistant, state: AuroraState) -> None:
        self._hass = hass
        self._state = state

    async def post(self, request: web.Request, operation: str) -> web.Response:
        if not await _admin(request):
            return _error(403, "admin_required")
        if not self._state.admit_request():
            return _error(429, "rate_limited")
        async with self._state.lock:
            try:
                body = await asyncio.wait_for(_body(request), REQUEST_TIMEOUT_SECONDS)
                if body is None:
                    return _error(400, "invalid_body")
                if operation in {"promote-home-command", "rollback-home-command"}:
                    try:
                        await _reconcile_production_transition(
                            self._hass, self._state
                        )
                    except ValueError:
                        return _error(409, "production_recovery_required")
                if operation == "bootstrap":
                    created, target = await _ensure_preview(self._hass)
                    return _json_response({"dashboard_target": target, "created": created, "production_unchanged": True})
                if operation == "stage":
                    return await self._stage(body)
                if operation == "promote-home-command":
                    return await self._promote(body)
                if operation == "rollback-home-command":
                    return await self._rollback()
            except ValueError as exc:
                return _error(422, str(exc))
            except (OSError, RuntimeError):
                return _error(500, "adapter_failure")
        return _error(404, "not_found")

    async def _stage(self, body: dict[str, Any]) -> web.Response:
        if body.get("dashboard_target") != PREVIEW or body.get("preview_only") is not True:
            return _error(422, "fixed_target_required")
        package = _decode_b64((body.get("artifacts") or {}).get("package"), MAX_PACKAGE)
        dashboard = _decode_b64((body.get("artifacts") or {}).get("dashboard"), MAX_DASHBOARD)
        manifest_sha, package_sha, dashboard_sha = _validate_manifest(self._hass, body.get("manifest"), package, dashboard)
        manifest = body["manifest"]
        nonce = manifest["nonce"]
        used = self._state.journal.setdefault("nonces", {})
        if nonce in used:
            return _error(409, "manifest_replay")
        revision = hashlib.sha256((manifest_sha + package_sha + dashboard_sha).encode()).hexdigest()[:32]
        transaction_id = "tx-" + secrets.token_hex(16)
        revision_dir = self._state.root / "staged" / revision
        revision_dir.mkdir(parents=True, exist_ok=False)
        _atomic_write(revision_dir / "manifest.json", _json_bytes(manifest))
        _atomic_write(revision_dir / "aurora-preview-package.tar.gz", package)
        _atomic_write(revision_dir / "aurora-preview-dashboard.js", dashboard)
        transaction = {
            "transaction_id": transaction_id,
            "revision": revision,
            "status": "verified",
            "manifest_sha256": manifest_sha,
            "package_sha256": package_sha,
            "dashboard_sha256": dashboard_sha,
            "target": PREVIEW,
            "created_at": _now().isoformat(),
            "expires_at": manifest["expires_at"],
        }
        self._state.journal.setdefault("transactions", {})[transaction_id] = transaction
        used[nonce] = transaction_id
        self._state.save()
        public_keys = (
            "transaction_id",
            "revision",
            "status",
            "manifest_sha256",
            "package_sha256",
            "dashboard_sha256",
            "target",
            "created_at",
            "expires_at",
        )
        return _json_response(
            {key: transaction[key] for key in public_keys} | {"staged_revision": revision}
        )

    async def _promote(  # noqa: C901 - explicit fail-closed promotion state machine
        self, body: dict[str, Any]
    ) -> web.Response:
        """Durably prepare, CAS-check, apply, and commit one promotion."""
        revision = body.get("preview_revision")
        if not isinstance(revision, str):
            return _error(422, "preview_revision_required")
        transaction = next(
            (
                item
                for item in self._state.journal.get("transactions", {}).values()
                if isinstance(item, dict)
                and item.get("revision") == revision
                and item.get("status") in {"activated", "reloaded", "promoted"}
            ),
            None,
        )
        if transaction is None or self._state.journal.get("active_preview") != revision:
            return _error(409, "preview_revision_not_active")
        try:
                await _verify_active_transaction(
                    self._hass, transaction, self._state.root
                )
        except (OSError, ValueError, RuntimeError):
            return _error(409, "preview_integrity_failed")
        _preview, preview_config = await _load_dashboard(self._hass, PREVIEW)
        production, production_config = await _load_dashboard(self._hass, PRODUCTION)
        preview_config_sha = _config_sha256(preview_config)
        production_config_sha = _config_sha256(production_config)
        expected_production_revision = _ensure_production_baseline(
            self._state, production_config_sha
        )
        if body.get("inspect") is True:
            return _json_response(
                {
                    "preview_revision": revision,
                    "status": transaction.get("status"),
                    "active_revision": self._state.journal.get("active_preview"),
                    "target_dashboard": PRODUCTION,
                    "preview_config_sha256": preview_config_sha,
                    "production_revision": expected_production_revision,
                    "expected_production_config_sha256": production_config_sha,
                    "verified": True,
                }
            )
        if (
            transaction.get("status") == "promoted"
            and self._state.journal.get("production_revision") == revision
            and production_config_sha == preview_config_sha
        ):
            previous = self._state.journal.get("previous_production")
            return _json_response(
                {
                    "promoted": True,
                    "active_revision": revision,
                    "previous_revision": (
                        previous.get("revision")
                        if isinstance(previous, dict)
                        else None
                    ),
                    "preview_config_sha256": preview_config_sha,
                    "expected_production_config_sha256": production_config_sha,
                }
            )
        receipt = body.get("receipt")
        if not isinstance(receipt, dict):
            return _error(422, "validation_receipt_required")
        if set(receipt) != VALIDATION_RECEIPT_KEYS or receipt.get(
            "schema_version"
        ) != 1:
            return _error(422, "validation_receipt_schema_invalid")
        if receipt.get("preview_revision") != revision or receipt.get("dashboard_target") != PREVIEW or receipt.get("physical_validation") is not True:
            return _error(422, "validation_receipt_invalid")
        expected_production_revision = body.get("expected_production_revision")
        if not isinstance(expected_production_revision, str) or not expected_production_revision:
            return _error(422, "expected_production_revision_required")
        if receipt.get("expected_production_revision") != expected_production_revision:
            return _error(422, "validation_receipt_revision_mismatch")
        device_results = receipt.get("device_results")
        required_devices = {"mobile", "kiosk", "tablet", "laptop", "desktop"}
        if (
            not isinstance(device_results, list)
            or len(device_results) != len(required_devices)
            or {
                item.get("device_id")
                for item in device_results
                if isinstance(item, dict)
            }
                != required_devices
            or any(
                not isinstance(item, dict)
                or set(item) != {"device_id", "passed"}
                or item.get("passed") is not True
                for item in device_results
            )
        ):
            return _error(422, "validation_receipt_devices_invalid")
        issued_at = _parse_time(receipt.get("issued_at"))
        expires_at = _parse_time(receipt.get("expires_at"))
        now = _now()
        if (
            issued_at - CLOCK_SKEW > now
            or expires_at <= issued_at
            or expires_at <= now
            or expires_at - issued_at > MAX_LIFETIME
        ):
            return _error(422, "validation_receipt_time_invalid")
        receipt_nonce = receipt.get("nonce")
        if not isinstance(receipt_nonce, str) or _SAFE_NONCE.fullmatch(receipt_nonce) is None:
            return _error(422, "validation_receipt_nonce_invalid")
        used_receipts = self._state.journal.setdefault("receipt_nonces", {})
        if receipt_nonce in used_receipts:
            return _error(409, "validation_receipt_replay")
        _verify_signature(self._hass, receipt, prefix="validation-")
        if any(receipt.get(key) != transaction.get(key) for key in ("manifest_sha256", "package_sha256", "dashboard_sha256")):
            return _error(422, "validation_receipt_hash_mismatch")
        expected_revision = self._state.journal.get("production_revision")
        if expected_production_revision != expected_revision:
            return _error(409, "production_revision_conflict")
        if (
            not _is_sha256(receipt.get("preview_config_sha256"))
            or receipt.get("preview_config_sha256") != preview_config_sha
        ):
            return _error(422, "validation_receipt_preview_config_mismatch")
        if (
            not _is_sha256(receipt.get("expected_production_config_sha256"))
            or receipt.get("expected_production_config_sha256")
            != production_config_sha
        ):
            return _error(409, "production_config_conflict")
        transition = self._state.journal.get("production_transition")
        if isinstance(transition, dict) and transition.get("status") == "prepared":
            if (
                transition.get("to_revision") != revision
                or transition.get("expected_revision") != expected_revision
                or transition.get("expected_config_sha256")
                != production_config_sha
                or transition.get("next_config_sha256") != preview_config_sha
            ):
                return _error(409, "production_transition_in_progress")
            previous = transition.get("previous")
            next_config = transition.get("next_config")
            if not isinstance(previous, dict) or not isinstance(next_config, dict):
                return _error(409, "production_transition_invalid")
        else:
            previous = {
                "revision": expected_revision,
                "config": json.loads(_json_bytes(production_config)),
                "config_sha256": production_config_sha,
            }
            next_config = json.loads(_json_bytes(preview_config))
            transition = {
                "transition_id": "promotion-" + secrets.token_hex(16),
                "status": "prepared",
                "expected_revision": expected_revision,
                "expected_config_sha256": production_config_sha,
                "to_revision": revision,
                "transaction_id": transaction["transaction_id"],
                "receipt_nonce": receipt_nonce,
                "previous": previous,
                "next_config": next_config,
                "next_config_sha256": preview_config_sha,
                "prepared_at": _now().isoformat(),
            }
            self._state.journal["production_transition"] = transition
            self._state.save()
        if (
            self._state.journal.get("production_revision") != expected_revision
            or self._state.journal.get("production_transition") is not transition
        ):
            return _error(409, "production_revision_conflict")
        _preview_check, preview_check = await _load_dashboard(self._hass, PREVIEW)
        _production_check, production_check = await _load_dashboard(
            self._hass, PRODUCTION
        )
        if _config_sha256(preview_check) != preview_config_sha:
            return _error(409, "preview_config_conflict")
        if _config_sha256(production_check) != production_config_sha:
            return _error(409, "production_config_conflict")
        await production.async_save(next_config)
        if (
            self._state.journal.get("production_revision") != expected_revision
            or self._state.journal.get("production_transition") is not transition
        ):
            return _error(409, "production_revision_conflict")
        self._state.journal["previous_production"] = previous
        self._state.journal["production_revision"] = revision
        self._state.journal["production_config_sha256"] = preview_config_sha
        transaction["status"] = "promoted"
        transaction["promoted_at"] = _now().isoformat()
        used_receipts[receipt_nonce] = transaction["transaction_id"]
        transition["status"] = "committed"
        transition["committed_at"] = transaction["promoted_at"]
        self._state.save()
        return _json_response(
            {
                "promoted": True,
                "active_revision": revision,
                "previous_revision": previous.get("revision"),
                "preview_config_sha256": preview_config_sha,
                "expected_production_config_sha256": production_config_sha,
            }
        )

    async def _rollback(self) -> web.Response:
        previous = self._state.journal.get("previous_production")
        if (
            not isinstance(previous, dict)
            or not isinstance(previous.get("config"), dict)
            or not _is_sha256(previous.get("config_sha256"))
            or not _is_sha256(
                self._state.journal.get("production_config_sha256")
            )
        ):
            return _error(409, "no_prior_production_revision")
        production, current = await _load_dashboard(self._hass, PRODUCTION)
        if _config_sha256(current) != self._state.journal.get(
            "production_config_sha256"
        ):
            return _error(409, "production_config_conflict")
        if _config_sha256(previous["config"]) != previous["config_sha256"]:
            return _error(409, "production_rollback_evidence_invalid")
        await production.async_save(previous["config"])
        active = self._state.journal.get("production_revision")
        self._state.journal["production_revision"] = previous.get("revision")
        self._state.journal["production_config_sha256"] = previous[
            "config_sha256"
        ]
        self._state.journal["previous_production"] = None
        for transaction in self._state.journal.get("transactions", {}).values():
            if isinstance(transaction, dict) and transaction.get("revision") == active:
                transaction["status"] = "rolled_back"
                transaction["rolled_back_at"] = _now().isoformat()
        self._state.save()
        return _json_response({"rolled_back": True, "active_revision": previous.get("revision")})


class TransactionView(HomeAssistantView):
    """Readback, activation, and reload routes for one transaction."""

    requires_auth = True
    url = BASE + "/{transaction_id}/{operation}"
    name = DOMAIN + ":transaction"

    def __init__(self, hass: HomeAssistant, state: AuroraState) -> None:
        self._hass = hass
        self._state = state

    async def get(self, request: web.Request, transaction_id: str, operation: str) -> web.Response:
        if not await _admin(request):
            return _error(403, "admin_required")
        if not self._state.admit_request():
            return _error(429, "rate_limited")
        if operation != "readback":
            return _error(404, "not_found")
        transaction = self._state.tx(transaction_id)
        if transaction is None:
            return _error(404, "transaction_not_found")
        try:
            package, dashboard, _manifest = _verify_staged_artifacts(
                transaction, self._state.root
            )
        except (OSError, ValueError):
            return _error(409, "staged_integrity_failed")
        active_component_sha: str | None = None
        if transaction.get("status") in {"activated", "reloaded", "promoted"}:
            try:
                if package is None or dashboard is None:
                    raise ValueError("staged_artifact_missing")
                members = _validate_package(package)
                active_component_sha = await _verify_active_bindings(
                    self._hass, transaction, members, dashboard
                )
            except (OSError, ValueError, RuntimeError):
                return _error(409, "active_integrity_failed")
        payload = {
            key: transaction.get(key)
            for key in (
                "transaction_id",
                "revision",
                "status",
                "manifest_sha256",
                "package_sha256",
                "dashboard_sha256",
                "target",
            )
        }
        payload["verified"] = transaction.get("status") in {
            "verified",
            "activated",
            "reloaded",
            "promoted",
        }
        if active_component_sha is not None:
            payload["active_package_sha256"] = active_component_sha
            payload["active_package_verified"] = True
            payload["active_dashboard_verified"] = True
        return _json_response(payload)

    async def post(self, request: web.Request, transaction_id: str, operation: str) -> web.Response:
        if not await _admin(request):
            return _error(403, "admin_required")
        if not self._state.admit_request():
            return _error(429, "rate_limited")
        async with self._state.lock:
            transaction = self._state.tx(transaction_id)
            if transaction is None:
                return _error(404, "transaction_not_found")
            try:
                if operation == "activate":
                    if transaction.get("status") != "verified":
                        return _error(409, "transaction_not_verified")
                    package, dashboard, _manifest = _verify_staged_artifacts(
                        transaction, self._state.root
                    )
                    if package is None or dashboard is None:
                        raise ValueError("staged_artifact_missing")
                    component_members = _validate_package(package)
                    await _ensure_preview(self._hass)
                    preview, preview_config = await _load_dashboard(self._hass, PREVIEW)
                    transaction["preview_before"] = json.loads(_json_bytes(preview_config))
                    transaction["preview_revision_before"] = self._state.journal.get(
                        "active_preview"
                    )
                    component_activated = False
                    try:
                        await asyncio.to_thread(
                            _activate_component_package,
                            self._hass,
                            self._state,
                            transaction,
                            component_members,
                        )
                        component_activated = True
                        asset_name = await _save_preview_asset(self._hass, dashboard)
                    except (OSError, ValueError, RuntimeError):
                        if component_activated:
                            await asyncio.to_thread(
                                _restore_component_prestate,
                                self._hass,
                                self._state,
                                transaction,
                            )
                        try:
                            await preview.async_save(transaction["preview_before"])
                        except (OSError, ValueError, RuntimeError):
                            pass
                        raise
                    transaction["active_dashboard_asset"] = asset_name
                    self._state.journal["active_preview"] = transaction["revision"]
                    transaction["status"] = "activated"
                    transaction["activated_at"] = _now().isoformat()
                    self._state.save()
                    return _json_response({"activated": True, "active_revision": transaction["revision"], "status": "activated"})
                if operation == "rollback":
                    if transaction.get("status") not in {"activated", "reloaded"}:
                        return _error(409, "transaction_not_rollbackable")
                    previous_preview = transaction.get("preview_before")
                    if not isinstance(previous_preview, dict):
                        return _error(409, "preview_rollback_snapshot_missing")
                    preview, _current = await _load_dashboard(self._hass, PREVIEW)
                    await preview.async_save(previous_preview)
                    await asyncio.to_thread(
                        _restore_component_prestate,
                        self._hass,
                        self._state,
                        transaction,
                    )
                    if self._state.journal.get("active_preview") == transaction.get("revision"):
                        previous_revision = transaction.get("preview_revision_before")
                        if isinstance(previous_revision, str):
                            self._state.journal["active_preview"] = previous_revision
                        else:
                            self._state.journal.pop("active_preview", None)
                    transaction["status"] = "rolled_back"
                    transaction["rolled_back_at"] = _now().isoformat()
                    self._state.save()
                    active_revision = self._state.journal.get("active_preview")
                    return _json_response(
                        {
                            "rolled_back": True,
                            "active_revision": active_revision,
                            "preview_active": isinstance(active_revision, str),
                            "status": "rolled_back",
                        }
                    )
                if operation == "reload":
                    if transaction.get("status") != "activated":
                        return _error(409, "transaction_not_activated")
                    return _error(409, "restart_required")
            except (OSError, ValueError, RuntimeError):
                return _error(500, "activation_failed")
        return _error(404, "not_found")
