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
import uuid
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
PREVIEW = "aurora-preview"
PRODUCTION = "home-command"
# This obsolete candidate target is a fixed collision, not a caller-selectable
# dashboard id; refusing it prevents bootstrap from creating a third environment.
LEGACY_PREVIEW = "home-command-preview"
TARGET = "aurora-v9-preview"
APPROVED_RELEASE = "0.1.16"
APPROVED_PREVIEW_DISPLAY_PAIRS = frozenset(
    {
        ("Aurora Preview", "mdi:aurora"),
        ("Aurora V9 Preview", "mdi:home-analytics"),
    }
)
MAX_BODY = 80 * 1024 * 1024
MAX_MANIFEST = 512 * 1024
MAX_PACKAGE = 64 * 1024 * 1024
MAX_DASHBOARD = 8 * 1024 * 1024
MAX_MEMBERS = 256
MAX_EXPANDED = 32 * 1024 * 1024
CLOCK_SKEW = timedelta(minutes=5)
MAX_LIFETIME = timedelta(hours=24)
MAX_AUTOMATED_E2E_LIFETIME = timedelta(minutes=10)
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
        "synthetic_fixture.py",
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
VALIDATION_RECEIPT_V1_KEYS = frozenset(
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
VALIDATION_RECEIPT_V2_KEYS = frozenset(
    {
        "schema_version",
        "preview_revision",
        "transaction_id",
        "operation_id",
        "expected_production_revision",
        "preview_config_sha256",
        "expected_production_config_sha256",
        "dashboard_target",
        "validation_kind",
        "e2e_evidence_sha256",
        "profile_results",
        "manifest_sha256",
        "package_sha256",
        "dashboard_sha256",
        "audience",
        "action",
        "issued_at",
        "expires_at",
        "nonce",
        "signer",
        "signature",
    }
)
AUTOMATED_E2E_PROFILES = (
    ("mobile-390x844", 390, 844),
    ("tablet-portrait-800x1280", 800, 1280),
    ("tablet-landscape-1280x800", 1280, 800),
    ("kiosk-1280x800", 1280, 800),
    ("laptop-1100x800", 1100, 800),
    ("desktop-1440x1000", 1440, 1000),
)
DASHBOARD_URL = "/local/aurora/aurora-preview-dashboard.js"
DASHBOARD_URL_PREFIX = "/local/aurora/revisions/"
_CONTENT_ADDRESSED_DASHBOARD_URL = re.compile(
    re.escape(DASHBOARD_URL_PREFIX)
    + r"aurora-preview-dashboard-([0-9a-f]{64})\.js"
)
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


def _is_uuid_operation_id(value: Any) -> bool:
    """Return whether value is a canonical RFC 4122 UUID identifier."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.variant == uuid.RFC_4122 and str(parsed) == value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


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


def _verify_signature(
    hass: HomeAssistant, document: dict[str, Any], *, prefix: str
) -> None:
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
            Ed25519PublicKey.from_public_bytes(public).verify(
                signature, _canonical_manifest(document)
            )
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
                if (
                    member.issym()
                    or member.islnk()
                    or member.isdev()
                    or member.isfifo()
                ):
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
            installation.get("installer") != "install-aurora-camera-ai-component.py",
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
        if (
            entry.get("size") != len(data)
            or entry.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
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
            installer.get("sha256") != hashlib.sha256(installer_bytes).hexdigest(),
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


async def _verify_active_bindings(
    hass: HomeAssistant,
    transaction: dict[str, Any],
    dashboard_bytes: bytes,
) -> str:
    """Verify only the content-addressed preview dashboard deployment."""
    dashboard_sha = hashlib.sha256(dashboard_bytes).hexdigest()
    resource_context = await asyncio.to_thread(
        _active_resource_context, hass, transaction
    )
    if resource_context["preview_resource_sha256"] != dashboard_sha:
        raise ValueError("active_dashboard_binding")
    asset_name = transaction["active_dashboard_asset"]
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
    return dashboard_sha


async def _verify_active_transaction(
    hass: HomeAssistant, transaction: dict[str, Any], root: Path | None = None
) -> str:
    package, dashboard, _manifest = _verify_staged_artifacts(transaction, root)
    if package is None or dashboard is None:
        raise ValueError("staged_artifact_missing")
    _validate_package(package)
    return await _verify_active_bindings(hass, transaction, dashboard)


def _validate_manifest(  # noqa: C901 - fail-closed policy checks are kept together
    hass: HomeAssistant, manifest: Any, package: bytes, dashboard: bytes
) -> tuple[str, str, str]:
    if not isinstance(manifest, dict) or len(_json_bytes(manifest)) > MAX_MANIFEST:
        raise ValueError("manifest")
    if manifest.get("schema_version") != 1 or manifest.get("target") != TARGET:
        raise ValueError("target")
    if (
        manifest.get("dashboard_target", PREVIEW) != PREVIEW
        or manifest.get("preview_only") is not True
    ):
        raise ValueError("dashboard")
    if manifest.get("target_release") != APPROVED_RELEASE:
        raise ValueError("release")
    if manifest.get("privacy_policy") != "no-sensitive-inference-v1":
        raise ValueError("privacy_policy")
    issued = _parse_time(manifest.get("issued_at", manifest.get("created_at")))
    expires = _parse_time(manifest.get("expires_at"))
    now = _now()
    if (
        issued - CLOCK_SKEW > now
        or expires + CLOCK_SKEW < now
        or expires <= issued
        or expires - issued > MAX_LIFETIME
    ):
        raise ValueError("expiry")
    nonce = manifest.get("nonce")
    if (
        not isinstance(nonce, str)
        or not (8 <= len(nonce) <= 128)
        or any(
            ch
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for ch in nonce
        )
    ):
        raise ValueError("nonce")
    _verify_signature(hass, manifest, prefix="release-")
    package_hash = hashlib.sha256(package).hexdigest()
    dashboard_hash = hashlib.sha256(dashboard).hexdigest()
    if (
        manifest.get("artifact_sha256", manifest.get("package_sha256")) != package_hash
        or manifest.get("dashboard_sha256") != dashboard_hash
    ):
        raise ValueError("hash")
    assets = manifest.get("assets")
    expected = {
        "aurora-preview-package": package_hash,
        "aurora-preview-dashboard": dashboard_hash,
    }
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("assets")
    seen: set[str] = set()
    for item in assets:
        if (
            not isinstance(item, dict)
            or item.get("name") not in expected
            or item["name"] in seen
            or item.get("sha256") != expected[item["name"]]
        ):
            raise ValueError("assets")
        seen.add(item["name"])
    if seen != set(expected):
        raise ValueError("assets")
    _scan_privacy(_json_bytes(manifest))
    _scan_privacy(dashboard)
    _validate_package(package)
    return (
        hashlib.sha256(_canonical_manifest(manifest)).hexdigest(),
        package_hash,
        dashboard_hash,
    )


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


def _verify_staged_artifacts(  # noqa: C901 - staged evidence checks are fail-closed
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
    if path.is_symlink():
        raise ValueError("dashboard_asset_collision")
    if path.exists():
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != len(data)
            or path.read_bytes() != data
        ):
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
            journal = (
                json.loads(journal_path.read_text(encoding="utf-8"))
                if journal_path.exists()
                else {}
            )
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
        while (
            self.request_times and now - self.request_times[0] >= REQUEST_WINDOW_SECONDS
        ):
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
            state.save()
        return current
    baseline = "baseline-" + config_sha256
    state.journal["production_revision"] = baseline
    state.journal["production_config_sha256"] = config_sha256
    state.save()
    return baseline


def _deployment_audience(state: AuroraState) -> str:
    """Return a stable public identifier for receipts issued to this HA instance."""
    audience = state.journal.get("audience")
    if audience is None:
        audience = "urn:home-assistant:aurora-deploy:" + secrets.token_hex(16)
        state.journal["audience"] = audience
        state.save()
    if (
        not isinstance(audience, str)
        or re.fullmatch(r"urn:home-assistant:aurora-deploy:[0-9a-f]{32}", audience)
        is None
    ):
        raise ValueError("deployment_audience_invalid")
    return audience


def _active_resource_context(
    hass: HomeAssistant, transaction: dict[str, Any]
) -> dict[str, Any]:
    """Return bounded public resource evidence after exact path/hash validation."""
    dashboard_sha = transaction.get("dashboard_sha256")
    if not _is_sha256(dashboard_sha):
        raise ValueError("active_dashboard_binding")
    asset_name = f"aurora-preview-dashboard-{dashboard_sha}.js"
    if transaction.get("active_dashboard_asset") != asset_name:
        raise ValueError("active_dashboard_binding")
    path = Path(hass.config.path(f"www/aurora/revisions/{asset_name}"))
    if path.is_symlink() or not path.is_file():
        raise ValueError("active_dashboard_binding")
    raw = _read_staged_file(path, MAX_DASHBOARD)
    if hashlib.sha256(raw).hexdigest() != dashboard_sha:
        raise ValueError("active_dashboard_binding")
    return {
        "preview_resource_url": DASHBOARD_URL_PREFIX + asset_name,
        "preview_resource_sha256": dashboard_sha,
        "preview_resource_size": len(raw),
    }


def _dashboard_resource_binding_from_config(
    hass: HomeAssistant, config: dict[str, Any]
) -> dict[str, Any]:
    """Rehash the sole content-addressed Aurora resource, if one is configured."""
    resources = config.get("resources")
    if not isinstance(resources, list):
        return {}
    candidates = [
        item
        for item in resources
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and (
            item["url"] == DASHBOARD_URL
            or item["url"].startswith(DASHBOARD_URL_PREFIX)
        )
    ]
    if not candidates:
        return {}
    if len(candidates) != 1:
        raise ValueError("dashboard_resource_binding")
    item = candidates[0]
    url = item.get("url")
    match = (
        _CONTENT_ADDRESSED_DASHBOARD_URL.fullmatch(url)
        if isinstance(url, str)
        else None
    )
    if match is None or item.get("res_type") != "module":
        raise ValueError("dashboard_resource_binding")
    dashboard_sha = match.group(1)
    asset_name = url.removeprefix(DASHBOARD_URL_PREFIX)
    path = Path(hass.config.path(f"www/aurora/revisions/{asset_name}"))
    if path.is_symlink() or not path.is_file():
        raise ValueError("dashboard_resource_binding")
    raw = _read_staged_file(path, MAX_DASHBOARD)
    if hashlib.sha256(raw).hexdigest() != dashboard_sha:
        raise ValueError("dashboard_resource_binding")
    return {
        "dashboard_resource_url": url,
        "dashboard_sha256": dashboard_sha,
        "dashboard_size": len(raw),
    }


def _verify_recorded_resource_binding(
    hass: HomeAssistant,
    config: dict[str, Any],
    record: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    """Verify a live config's optional asset binding against one durable record."""
    actual = _dashboard_resource_binding_from_config(hass, config)
    recorded = {
        key: record.get(prefix + key)
        for key in (
            "dashboard_resource_url",
            "dashboard_sha256",
            "dashboard_size",
        )
        if record.get(prefix + key) is not None
    }
    if actual != recorded:
        raise ValueError("dashboard_resource_binding")
    return actual


def _verify_preview_revision_resource_binding(
    hass: HomeAssistant,
    state: AuroraState,
    revision: Any,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Bind restored preview config and asset bytes to its journaled revision."""
    binding = _dashboard_resource_binding_from_config(hass, config)
    if revision is None:
        if binding:
            raise ValueError("preview_revision_binding")
        return {}
    if not isinstance(revision, str):
        raise ValueError("preview_revision_binding")
    previous = next(
        (
            item
            for item in state.journal.get("transactions", {}).values()
            if isinstance(item, dict) and item.get("revision") == revision
        ),
        None,
    )
    if (
        previous is None
        or not binding
        or previous.get("dashboard_sha256") != binding.get("dashboard_sha256")
        or previous.get("active_dashboard_asset")
        != binding["dashboard_resource_url"].removeprefix(DASHBOARD_URL_PREFIX)
        or previous.get("active_preview_config_sha256") != _config_sha256(config)
    ):
        raise ValueError("preview_revision_binding")
    return binding


def _dashboard_config_with_asset(
    config: dict[str, Any], asset_name: str
) -> dict[str, Any]:
    """Return a canonical copy with exactly one Aurora revision resource."""
    updated = json.loads(_json_bytes(config))
    resources = updated.get("resources")
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
    resources.append(
        {
            "url": DASHBOARD_URL_PREFIX + asset_name,
            "res_type": "module",
        }
    )
    updated["resources"] = resources
    return updated


def _config_has_exact_dashboard_resource(
    config: dict[str, Any], expected_url: str
) -> bool:
    resources = config.get("resources")
    return isinstance(resources, list) and any(
        isinstance(item, dict)
        and set(item) >= {"url", "res_type"}
        and item.get("url") == expected_url
        and item.get("res_type") == "module"
        for item in resources
    )


def _verify_operation_resource_binding(
    hass: HomeAssistant,
    state: AuroraState,
    operation: dict[str, Any],
    production_config: dict[str, Any],
) -> dict[str, Any]:
    """Rehash the exact promoted asset and bind it to production config."""
    transaction_id = operation.get("transaction_id")
    transaction = state.tx(transaction_id) if isinstance(transaction_id, str) else None
    expected_url = operation.get("dashboard_resource_url")
    expected_sha = operation.get("dashboard_sha256")
    expected_size = operation.get("dashboard_size")
    if (
        transaction is None
        or transaction.get("dashboard_sha256") != expected_sha
        or not isinstance(expected_url, str)
        or not _is_sha256(expected_sha)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or not _config_has_exact_dashboard_resource(production_config, expected_url)
    ):
        raise ValueError("operation_dashboard_binding")
    context = _active_resource_context(hass, transaction)
    if any(
        (
            context["preview_resource_url"] != expected_url,
            context["preview_resource_sha256"] != expected_sha,
            context["preview_resource_size"] != expected_size,
        )
    ):
        raise ValueError("operation_dashboard_binding")
    return {
        "dashboard_resource_url": expected_url,
        "dashboard_sha256": expected_sha,
        "dashboard_size": expected_size,
    }


async def _admin(request: web.Request) -> bool:
    user = request.get("hass_user")
    return bool(user is not None and getattr(user, "is_admin", False))


async def _body(request: web.Request) -> dict[str, Any] | None:
    content_length = request.content_length
    if content_length is not None and (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length < 0
        or content_length > MAX_BODY
    ):
        return None
    raw = bytearray()
    while True:
        remaining = MAX_BODY - len(raw)
        chunk = await request.content.read(min(64 * 1024, remaining + 1))
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            return None
        if not chunk:
            break
        if len(chunk) > remaining:
            return None
        raw.extend(chunk)
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
        fixed_metadata = {
            CONF_URL_PATH: PREVIEW,
            CONF_SHOW_IN_SIDEBAR: False,
            CONF_REQUIRE_ADMIN: True,
        }
        display_pair = (config.get(CONF_TITLE), config.get(CONF_ICON))
        if any(
            config.get(key) != value for key, value in fixed_metadata.items()
        ) or display_pair not in APPROVED_PREVIEW_DISPLAY_PAIRS:
            raise ValueError("preview_collision")
        return False, PREVIEW
    from homeassistant.components.lovelace import _register_panel, dashboard

    collection = dashboard.DashboardsCollection(hass)
    await collection.async_load()
    item = await collection.async_create_item(dict(metadata))
    dashboards[PREVIEW] = dashboard.LovelaceStorage(hass, item)
    _register_panel(hass, PREVIEW, dashboard.MODE_STORAGE, item, False)
    return True, PREVIEW


async def _load_dashboard(
    hass: HomeAssistant, url_path: str
) -> tuple[Any, dict[str, Any]]:
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
    dashboard, config = await _load_dashboard(hass, PREVIEW)
    await dashboard.async_save(_dashboard_config_with_asset(config, asset_name))
    return asset_name


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
    operation_id = transition.get("operation_id")
    receipt_nonce = transition.get("receipt_nonce")
    expected_revision = transition.get("expected_revision")
    expected_config_sha = transition.get("expected_config_sha256")
    next_sha = transition.get("next_config_sha256")
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("revision"), str)
        or not isinstance(previous.get("config"), dict)
        or not _is_sha256(previous.get("config_sha256"))
        or not isinstance(next_config, dict)
        or not isinstance(expected_revision, str)
        or not _is_sha256(expected_config_sha)
        or not isinstance(transition.get("to_revision"), str)
        or not _is_sha256(next_sha)
        or not isinstance(transaction_id, str)
        or not isinstance(operation_id, str)
        or not isinstance(receipt_nonce, str)
        or previous.get("revision") != expected_revision
        or previous.get("config_sha256") != expected_config_sha
        or _config_sha256(previous["config"]) != expected_config_sha
        or _config_sha256(next_config) != next_sha
    ):
        raise ValueError("production_transition_recovery_invalid")
    transaction = state.tx(transaction_id)
    operation = state.journal.get("operations", {}).get(operation_id)
    if (
        transaction is None
        or transaction.get("revision") != transition.get("to_revision")
        or not isinstance(operation, dict)
        or state.journal.get("active_preview") != transition.get("to_revision")
        or state.journal.get("production_revision") != expected_revision
        or state.journal.get("production_config_sha256") != expected_config_sha
        or operation.get("operation_id") != operation_id
        or operation.get("action") != "promote_home_command"
        or operation.get("status") != "prepared"
        or operation.get("transaction_id") != transaction_id
        or operation.get("preview_revision") != transition.get("to_revision")
        or operation.get("target_revision") != transition.get("to_revision")
        or operation.get("expected_production_revision") != expected_revision
        or operation.get("expected_production_config_sha256")
        != expected_config_sha
        or operation.get("preview_config_sha256") != next_sha
        or operation.get("receipt_sha256") != transition.get("receipt_sha256")
        or not _is_sha256(operation.get("receipt_sha256"))
        or operation.get("request_sha256") != transition.get("request_sha256")
        or not _is_sha256(operation.get("request_sha256"))
        or transaction.get("status") not in {"activated", "reloaded"}
        or state.journal.get("receipt_nonces", {}).get(receipt_nonce)
        != transaction_id
        or any(
            operation.get(key) != transition.get(key)
            for key in (
                "dashboard_resource_url",
                "dashboard_sha256",
                "dashboard_size",
                "expected_dashboard_resource_url",
                "expected_dashboard_sha256",
                "expected_dashboard_size",
            )
        )
    ):
        raise ValueError("production_transition_recovery_invalid")
    _production, current_config = await _load_dashboard(hass, PRODUCTION)
    current_sha = _config_sha256(current_config)
    previous_sha = previous["config_sha256"]
    if current_sha == next_sha:
        _verify_operation_resource_binding(hass, state, operation, current_config)
        committed_at = _now().isoformat()
        state.journal["previous_production"] = previous
        state.journal["production_revision"] = transition["to_revision"]
        state.journal["production_config_sha256"] = next_sha
        state.journal.setdefault("receipt_nonces", {})[receipt_nonce] = transaction_id
        transaction["status"] = "promoted"
        transaction["promoted_at"] = committed_at
        transaction["promotion_operation_id"] = operation_id
        transition["status"] = "committed"
        transition["committed_at"] = committed_at
        transition["recovered"] = True
        operation["status"] = "committed"
        operation["production_config_sha256"] = next_sha
        operation["completed_at"] = committed_at
        operation["recovered"] = True
    elif current_sha == previous_sha:
        _verify_recorded_resource_binding(
            hass, current_config, transition, prefix="expected_"
        )
        transition["status"] = "aborted"
        transition["aborted_at"] = _now().isoformat()
        transition["recovered"] = True
        operation["status"] = "aborted"
        operation["completed_at"] = transition["aborted_at"]
        operation["recovered"] = True
    else:
        raise ValueError("production_transition_recovery_conflict")
    state.journal.pop("production_recovery_required", None)
    state.save()


def _commit_rollback_transition(
    state: AuroraState,
    transition: dict[str, Any],
    operation: dict[str, Any],
    *,
    recovered: bool = False,
) -> None:
    """Commit journal state only after production readback matches rollback bytes."""
    completed_at = _now().isoformat()
    from_revision = transition["from_revision"]
    to_revision = transition["to_revision"]
    next_sha = transition["next_config_sha256"]
    state.journal["production_revision"] = to_revision
    state.journal["production_config_sha256"] = next_sha
    state.journal["previous_production"] = None
    for transaction in state.journal.get("transactions", {}).values():
        if (
            isinstance(transaction, dict)
            and transaction.get("revision") == from_revision
        ):
            transaction["status"] = "rolled_back"
            transaction["rolled_back_at"] = completed_at
    transition["status"] = "committed"
    transition["committed_at"] = completed_at
    operation["status"] = "committed"
    operation["production_config_sha256"] = next_sha
    operation["completed_at"] = completed_at
    if recovered:
        transition["recovered"] = True
        operation["recovered"] = True
    state.save()


async def _reconcile_rollback_transition(
    hass: HomeAssistant, state: AuroraState
) -> None:
    """Resolve one durable prepared rollback from live production bytes."""
    transition = state.journal.get("rollback_transition")
    if not isinstance(transition, dict) or transition.get("status") != "prepared":
        return
    operation_id = transition.get("operation_id")
    operation = state.journal.get("operations", {}).get(operation_id)
    previous = state.journal.get("previous_production")
    if any(
        (
            not isinstance(operation_id, str),
            not isinstance(operation, dict),
            not isinstance(previous, dict),
            not isinstance(previous.get("config"), dict),
            not _is_sha256(previous.get("config_sha256")),
            not isinstance(transition.get("from_revision"), str),
            not isinstance(transition.get("to_revision"), str),
            not _is_sha256(transition.get("expected_config_sha256")),
            not _is_sha256(transition.get("next_config_sha256")),
            not isinstance(transition.get("next_config"), dict),
        )
    ):
        raise ValueError("rollback_transition_recovery_invalid")
    if any(
        (
            _config_sha256(transition["next_config"])
            != transition["next_config_sha256"],
            previous.get("revision") != transition.get("to_revision"),
            previous.get("config_sha256") != transition.get("next_config_sha256"),
            _config_sha256(previous["config"])
            != transition.get("next_config_sha256"),
            state.journal.get("production_revision")
            != transition.get("from_revision"),
            state.journal.get("production_config_sha256")
            != transition.get("expected_config_sha256"),
            operation.get("operation_id") != operation_id,
            operation.get("action") != "rollback_home_command",
            operation.get("status") != "prepared",
            operation.get("expected_production_revision")
            != transition.get("from_revision"),
            operation.get("expected_production_config_sha256")
            != transition.get("expected_config_sha256"),
            operation.get("target_revision") != transition.get("to_revision"),
            operation.get("request_sha256") != transition.get("request_sha256"),
            not _is_sha256(operation.get("request_sha256")),
            any(
                operation.get(key) != transition.get(key)
                for key in (
                    "dashboard_resource_url",
                    "dashboard_sha256",
                    "dashboard_size",
                    "expected_dashboard_resource_url",
                    "expected_dashboard_sha256",
                    "expected_dashboard_size",
                )
            ),
        )
    ):
        raise ValueError("rollback_transition_recovery_invalid")
    _production, current_config = await _load_dashboard(hass, PRODUCTION)
    current_sha = _config_sha256(current_config)
    if current_sha == transition["next_config_sha256"]:
        _verify_recorded_resource_binding(hass, current_config, transition)
        _commit_rollback_transition(state, transition, operation, recovered=True)
    elif current_sha == transition["expected_config_sha256"]:
        _verify_recorded_resource_binding(
            hass, current_config, transition, prefix="expected_"
        )
        completed_at = _now().isoformat()
        transition["status"] = "aborted"
        transition["aborted_at"] = completed_at
        transition["recovered"] = True
        operation["status"] = "aborted"
        operation["completed_at"] = completed_at
        operation["recovered"] = True
        state.save()
    else:
        raise ValueError("rollback_transition_recovery_conflict")


def _commit_preview_activation(
    state: AuroraState,
    transaction: dict[str, Any],
    transition: dict[str, Any],
    *,
    recovered: bool = False,
) -> None:
    completed_at = _now().isoformat()
    state.journal["active_preview"] = transaction["revision"]
    transaction["status"] = "activated"
    transaction["activated_at"] = completed_at
    transaction["active_preview_config_sha256"] = transition["next_config_sha256"]
    transition["status"] = "committed"
    transition["committed_at"] = completed_at
    if recovered:
        transition["recovered"] = True
    state.save()


def _commit_preview_rollback(
    state: AuroraState,
    transaction: dict[str, Any],
    transition: dict[str, Any],
    *,
    recovered: bool = False,
) -> None:
    completed_at = _now().isoformat()
    previous_revision = transition.get("to_revision")
    if isinstance(previous_revision, str):
        state.journal["active_preview"] = previous_revision
    else:
        state.journal.pop("active_preview", None)
    transaction["status"] = "rolled_back"
    transaction["rolled_back_at"] = completed_at
    transition["status"] = "committed"
    transition["committed_at"] = completed_at
    if recovered:
        transition["recovered"] = True
    state.save()


def _reconcile_stage_transition(
    state: AuroraState, transaction: dict[str, Any]
) -> None:
    """Settle a caller-addressable prepared stage from exact staged bytes."""
    transition = transaction.get("stage_transition")
    if not isinstance(transition, dict) or transition.get("status") != "prepared":
        return
    if any(
        (
            transition.get("transaction_id") != transaction.get("transaction_id"),
            transition.get("revision") != transaction.get("revision"),
            transition.get("request_sha256")
            != transaction.get("stage_request_sha256"),
            not _is_sha256(transition.get("request_sha256")),
            transition.get("manifest_sha256")
            != transaction.get("manifest_sha256"),
            transition.get("package_sha256") != transaction.get("package_sha256"),
            transition.get("dashboard_sha256")
            != transaction.get("dashboard_sha256"),
            transaction.get("status") != "staging",
        )
    ):
        raise ValueError("stage_transition_invalid")
    staged_root = state.root / "staged"
    revision_dir = staged_root / transaction["revision"]
    if not staged_root.exists() or not revision_dir.exists():
        if staged_root.is_symlink() or revision_dir.is_symlink():
            raise ValueError("stage_transition_invalid")
        completed_at = _now().isoformat()
        transition["status"] = "aborted"
        transition["aborted_at"] = completed_at
        transition["recovered"] = True
        transaction["status"] = "aborted"
        transaction["completed_at"] = completed_at
        state.save()
        return
    try:
        package, dashboard, manifest = _verify_staged_artifacts(
            transaction, state.root
        )
    except ValueError as exc:
        if str(exc) != "staged_artifact_missing":
            raise
        completed_at = _now().isoformat()
        transition["status"] = "aborted"
        transition["aborted_at"] = completed_at
        transition["recovered"] = True
        transaction["status"] = "aborted"
        transaction["completed_at"] = completed_at
        state.save()
        return
    if package is None or dashboard is None or manifest is None:
        raise ValueError("stage_transition_invalid")
    completed_at = _now().isoformat()
    transition["status"] = "committed"
    transition["committed_at"] = completed_at
    transition["recovered"] = True
    transaction["status"] = "verified"
    transaction["verified_at"] = completed_at
    state.save()


async def _reconcile_transaction_transition(  # noqa: C901 - fail-closed recovery
    hass: HomeAssistant,
    state: AuroraState,
    transaction: dict[str, Any],
) -> None:
    """Settle a prepared preview activation or rollback from live config."""
    _reconcile_stage_transition(state, transaction)
    activation = transaction.get("activation_transition")
    rollback = transaction.get("preview_rollback_transition")
    prepared = [
        ("activate", activation),
        ("rollback", rollback),
    ]
    prepared = [
        (kind, item)
        for kind, item in prepared
        if isinstance(item, dict) and item.get("status") == "prepared"
    ]
    if not prepared:
        return
    if len(prepared) != 1:
        raise ValueError("preview_transition_invalid")
    kind, transition = prepared[0]
    preview_before = transaction.get("preview_before")
    dashboard_sha = transaction.get("dashboard_sha256")
    expected_asset = (
        f"aurora-preview-dashboard-{dashboard_sha}.js"
        if _is_sha256(dashboard_sha)
        else None
    )
    if (
        not isinstance(preview_before, dict)
        or not _is_sha256(transition.get("previous_config_sha256"))
        or not _is_sha256(transition.get("next_config_sha256"))
        or transition.get("transaction_id") != transaction.get("transaction_id")
            or transition.get("action") != kind
            or expected_asset is None
            or transition.get("asset_name") != expected_asset
        or transaction.get("active_dashboard_asset") != expected_asset
        or _config_sha256(preview_before)
        != transaction.get("preview_config_sha256_before")
    ):
        raise ValueError("preview_transition_invalid")
    if kind == "activate":
        expected_next = _dashboard_config_with_asset(preview_before, expected_asset)
        if any(
            (
                transition.get("previous_revision")
                != transaction.get("preview_revision_before"),
                transition.get("next_revision") != transaction.get("revision"),
                transition.get("previous_config_sha256")
                != transaction.get("preview_config_sha256_before"),
                transition.get("next_config_sha256")
                != _config_sha256(expected_next),
                transaction.get("status") != "verified",
                state.journal.get("active_preview")
                != transition.get("previous_revision"),
            )
        ):
            raise ValueError("preview_transition_invalid")
    elif any(
        (
            transition.get("from_status") not in {"activated", "reloaded"},
            transaction.get("status") != transition.get("from_status"),
            transition.get("from_revision") != transaction.get("revision"),
            transition.get("to_revision")
            != transaction.get("preview_revision_before"),
            transition.get("previous_config_sha256")
            != transaction.get("active_preview_config_sha256"),
            transition.get("next_config_sha256")
            != transaction.get("preview_config_sha256_before"),
            state.journal.get("active_preview") != transition.get("from_revision"),
        )
    ):
        raise ValueError("preview_transition_invalid")
    _preview, current_config = await _load_dashboard(hass, PREVIEW)
    current_sha = _config_sha256(current_config)
    if current_sha == transition["next_config_sha256"]:
        if kind == "activate":
            await _verify_active_transaction(hass, transaction, state.root)
            _commit_preview_activation(state, transaction, transition, recovered=True)
        else:
            _verify_preview_revision_resource_binding(
                hass, state, transition.get("to_revision"), current_config
            )
            _commit_preview_rollback(state, transaction, transition, recovered=True)
    elif current_sha == transition["previous_config_sha256"]:
        if kind == "rollback":
            await _verify_active_transaction(hass, transaction, state.root)
        completed_at = _now().isoformat()
        transition["status"] = "aborted"
        transition["aborted_at"] = completed_at
        transition["recovered"] = True
        if kind == "activate":
            transaction["status"] = "verified"
            transaction.pop("active_dashboard_asset", None)
        else:
            transaction["status"] = transition["from_status"]
        state.save()
    else:
        raise ValueError("preview_transition_recovery_conflict")


async def _reconcile_operation_transition(
    hass: HomeAssistant,
    state: AuroraState,
    operation_id: str,
    operation: dict[str, Any],
) -> None:
    """Settle only the durable transition bound to one queried operation."""
    if operation.get("status") != "prepared":
        return
    action = operation.get("action")
    if action == "promote_home_command":
        transition = state.journal.get("production_transition")
        reconciler = _reconcile_production_transition
    elif action == "rollback_home_command":
        transition = state.journal.get("rollback_transition")
        reconciler = _reconcile_rollback_transition
    else:
        raise ValueError("operation_transition_invalid")
    if (
        not isinstance(transition, dict)
        or transition.get("status") != "prepared"
        or transition.get("operation_id") != operation_id
    ):
        raise ValueError("operation_transition_invalid")
    await reconciler(hass, state)


class RootView(HomeAssistantView):
    """Bootstrap, stage, promote, and rollback routes."""

    requires_auth = True
    url = BASE + "/{operation}"
    name = DOMAIN + ":root"

    def __init__(self, hass: HomeAssistant, state: AuroraState) -> None:
        self._hass = hass
        self._state = state

    async def post(  # noqa: C901 - explicit fixed-route dispatcher
        self, request: web.Request, operation: str
    ) -> web.Response:
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
                        await _reconcile_production_transition(self._hass, self._state)
                        if operation == "rollback-home-command":
                            await _reconcile_rollback_transition(
                                self._hass, self._state
                            )
                    except ValueError:
                        return _error(409, "production_recovery_required")
                if operation == "bootstrap":
                    created, target = await _ensure_preview(self._hass)
                    return _json_response(
                        {
                            "dashboard_target": target,
                            "created": created,
                            "production_unchanged": True,
                        }
                    )
                if operation == "stage":
                    return await self._stage(body)
                if operation == "promote-home-command":
                    return await self._promote(body)
                if operation == "rollback-home-command":
                    return await self._rollback(body)
            except ValueError as exc:
                return _error(422, str(exc))
            except (OSError, RuntimeError):
                return _error(500, "adapter_failure")
        return _error(404, "not_found")

    async def _stage(  # noqa: C901 - durable staged-artifact transaction
        self, body: dict[str, Any]
    ) -> web.Response:
        if set(body) != {
            "transaction_id",
            "dashboard_target",
            "preview_only",
            "manifest",
            "artifacts",
        }:
            transaction_id = body.get("transaction_id")
            if (
                isinstance(transaction_id, str)
                and self._state.tx(transaction_id) is not None
            ):
                return _error(409, "transaction_id_conflict")
            return _error(422, "stage_request_schema_invalid")
        if (
            body.get("dashboard_target") != PREVIEW
            or body.get("preview_only") is not True
        ):
            return _error(422, "fixed_target_required")
        transaction_id = body.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or _SAFE_NONCE.fullmatch(transaction_id) is None
        ):
            return _error(422, "transaction_id_required")
        if transaction_id in self._state.journal.get("operations", {}):
            return _error(409, "transaction_id_conflict")
        artifacts = body.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "package",
            "dashboard",
        }:
            return _error(422, "stage_request_schema_invalid")
        package = _decode_b64(artifacts.get("package"), MAX_PACKAGE)
        dashboard = _decode_b64(artifacts.get("dashboard"), MAX_DASHBOARD)
        manifest = body.get("manifest")
        if not isinstance(manifest, dict):
            return _error(422, "manifest")
        manifest_sha = hashlib.sha256(_canonical_manifest(manifest)).hexdigest()
        package_sha = hashlib.sha256(package).hexdigest()
        dashboard_sha = hashlib.sha256(dashboard).hexdigest()
        nonce = manifest.get("nonce")
        request_sha = hashlib.sha256(
            _json_bytes(
                {
                    "transaction_id": transaction_id,
                    "dashboard_target": PREVIEW,
                    "preview_only": True,
                    "manifest_sha256": manifest_sha,
                    "package_sha256": package_sha,
                    "dashboard_sha256": dashboard_sha,
                    "nonce": nonce,
                }
            )
        ).hexdigest()
        transactions = self._state.journal.setdefault("transactions", {})
        existing = transactions.get(transaction_id)
        if transaction_id in transactions:
            if (
                not isinstance(existing, dict)
                or existing.get("stage_request_sha256") != request_sha
            ):
                return _error(409, "transaction_id_conflict")
            try:
                _reconcile_stage_transition(self._state, existing)
                if existing.get("status") != "aborted":
                    package_readback, dashboard_readback, manifest_readback = (
                        _verify_staged_artifacts(existing, self._state.root)
                    )
                    if any(
                        item is None
                        for item in (
                            package_readback,
                            dashboard_readback,
                            manifest_readback,
                        )
                    ):
                        raise ValueError("staged_artifact_missing")
            except (OSError, ValueError):
                return _error(409, "staged_integrity_failed")
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
                {key: existing[key] for key in public_keys}
                | {
                    "staged_revision": existing["revision"],
                    "previous_revision": existing.get(
                        "preview_revision_before",
                        existing.get("stage_previous_revision"),
                    ),
                    "idempotent": True,
                }
            )
        manifest_sha, package_sha, dashboard_sha = _validate_manifest(
            self._hass, manifest, package, dashboard
        )
        nonce = manifest["nonce"]
        used = self._state.journal.setdefault("nonces", {})
        if nonce in used:
            return _error(409, "manifest_replay")
        revision = hashlib.sha256(
            (manifest_sha + package_sha + dashboard_sha).encode()
        ).hexdigest()[:32]
        prepared_at = _now().isoformat()
        transaction = {
            "transaction_id": transaction_id,
            "revision": revision,
            "status": "staging",
            "manifest_sha256": manifest_sha,
            "package_sha256": package_sha,
            "dashboard_sha256": dashboard_sha,
            "target": PREVIEW,
            "stage_request_sha256": request_sha,
            "stage_previous_revision": self._state.journal.get("active_preview"),
            "created_at": prepared_at,
            "expires_at": manifest["expires_at"],
            "stage_transition": {
                "status": "prepared",
                "transaction_id": transaction_id,
                "revision": revision,
                "request_sha256": request_sha,
                "manifest_sha256": manifest_sha,
                "package_sha256": package_sha,
                "dashboard_sha256": dashboard_sha,
                "prepared_at": prepared_at,
            },
        }
        transactions[transaction_id] = transaction
        used[nonce] = transaction_id
        self._state.save()
        revision_dir = self._state.root / "staged" / revision
        revision_dir.mkdir(parents=True, exist_ok=True)
        if revision_dir.is_symlink() or not revision_dir.is_dir():
            raise ValueError("staged_revision_invalid")
        _save_immutable_asset(revision_dir / "manifest.json", _json_bytes(manifest))
        _save_immutable_asset(
            revision_dir / "aurora-preview-package.tar.gz", package
        )
        _save_immutable_asset(
            revision_dir / "aurora-preview-dashboard.js", dashboard
        )
        package_readback, dashboard_readback, manifest_readback = (
            _verify_staged_artifacts(transaction, self._state.root)
        )
        if any(
            item is None
            for item in (package_readback, dashboard_readback, manifest_readback)
        ):
            raise ValueError("staged_artifact_missing")
        transaction["status"] = "verified"
        transaction["verified_at"] = _now().isoformat()
        transaction["stage_transition"]["status"] = "committed"
        transaction["stage_transition"]["committed_at"] = transaction["verified_at"]
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
            {key: transaction[key] for key in public_keys}
            | {
                "staged_revision": revision,
                "previous_revision": transaction.get("stage_previous_revision"),
            }
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
        transaction_id = transaction.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            return _error(409, "preview_integrity_failed")
        try:
            await _verify_active_transaction(self._hass, transaction, self._state.root)
            resource_context = await asyncio.to_thread(
                _active_resource_context, self._hass, transaction
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
        audience = _deployment_audience(self._state)
        if body.get("inspect") is True:
            if set(body) != {"preview_revision", "inspect"}:
                return _error(422, "promotion_inspection_schema_invalid")
            return _json_response(
                {
                    "preview_revision": revision,
                    "transaction_id": transaction_id,
                    "active_preview_transaction_id": transaction_id,
                    "status": transaction.get("status"),
                    "active_revision": self._state.journal.get("active_preview"),
                    "dashboard_target": PREVIEW,
                    "target_dashboard": PRODUCTION,
                    "preview_config_sha256": preview_config_sha,
                    "production_revision": expected_production_revision,
                    "expected_production_config_sha256": production_config_sha,
                    "manifest_sha256": transaction.get("manifest_sha256"),
                    "package_sha256": transaction.get("package_sha256"),
                    "dashboard_sha256": transaction.get("dashboard_sha256"),
                    "audience": audience,
                    "verified": True,
                    **resource_context,
                }
            )
        if (
            transaction.get("status") == "promoted"
            and self._state.journal.get("production_revision") == revision
            and production_config_sha == preview_config_sha
            and not isinstance(body.get("receipt"), dict)
        ):
            previous = self._state.journal.get("previous_production")
            return _json_response(
                {
                    "promoted": True,
                    "operation_id": transaction.get("promotion_operation_id"),
                    "status": "committed",
                    "active_revision": revision,
                    "previous_revision": (
                        previous.get("revision") if isinstance(previous, dict) else None
                    ),
                    "preview_config_sha256": preview_config_sha,
                    "expected_production_config_sha256": production_config_sha,
                }
            )
        receipt = body.get("receipt")
        if not isinstance(receipt, dict):
            return _error(422, "validation_receipt_required")
        if set(body) != {
            "preview_revision",
            "expected_production_revision",
            "operation_id",
            "receipt",
        }:
            claimed_operation_id = body.get("operation_id")
            if isinstance(claimed_operation_id, str) and claimed_operation_id in (
                self._state.journal.get("operations", {})
            ):
                return _error(409, "operation_id_conflict")
            return _error(422, "promotion_request_schema_invalid")
        schema_version = receipt.get("schema_version")
        if schema_version == 1:
            expected_receipt_keys = VALIDATION_RECEIPT_V1_KEYS
        elif schema_version == 2:
            expected_receipt_keys = VALIDATION_RECEIPT_V2_KEYS
        else:
            return _error(422, "validation_receipt_schema_invalid")
        if set(receipt) != expected_receipt_keys:
            return _error(422, "validation_receipt_schema_invalid")
        if (
            receipt.get("preview_revision") != revision
            or receipt.get("dashboard_target") != PREVIEW
        ):
            return _error(422, "validation_receipt_invalid")
        if schema_version == 1 and receipt.get("physical_validation") is not True:
            return _error(422, "validation_receipt_invalid")
        if schema_version == 2 and any(
            (
                receipt.get("transaction_id") != transaction.get("transaction_id"),
                not isinstance(receipt.get("operation_id"), str),
                _SAFE_NONCE.fullmatch(receipt.get("operation_id", "")) is None,
                body.get("operation_id") != receipt.get("operation_id"),
                receipt.get("validation_kind") != "automated_e2e",
                receipt.get("audience") != audience,
                receipt.get("action") != "promote_home_command",
                not _is_sha256(receipt.get("e2e_evidence_sha256")),
            )
        ):
            return _error(422, "validation_receipt_invalid")
        expected_production_revision = body.get("expected_production_revision")
        if (
            not isinstance(expected_production_revision, str)
            or not expected_production_revision
        ):
            return _error(422, "expected_production_revision_required")
        if receipt.get("expected_production_revision") != expected_production_revision:
            claimed_operation_id = body.get("operation_id")
            if isinstance(
                claimed_operation_id, str
            ) and claimed_operation_id in self._state.journal.get("operations", {}):
                return _error(409, "operation_id_conflict")
            return _error(422, "validation_receipt_revision_mismatch")
        if schema_version == 1:
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
        else:
            profile_results = receipt.get("profile_results")
            if (
                not isinstance(profile_results, list)
                or len(profile_results) != len(AUTOMATED_E2E_PROFILES)
                or any(
                    not isinstance(item, dict)
                    or set(item)
                    != {
                        "profile_id",
                        "width",
                        "height",
                        "passed",
                        "screenshot_sha256",
                    }
                    or item.get("profile_id") != profile_id
                    or item.get("width") != width
                    or isinstance(item.get("width"), bool)
                    or item.get("height") != height
                    or isinstance(item.get("height"), bool)
                    or item.get("passed") is not True
                    or not _is_sha256(item.get("screenshot_sha256"))
                    for item, (profile_id, width, height) in zip(
                        profile_results, AUTOMATED_E2E_PROFILES, strict=True
                    )
                )
            ):
                return _error(422, "validation_receipt_profiles_invalid")
        issued_at = _parse_time(receipt.get("issued_at"))
        expires_at = _parse_time(receipt.get("expires_at"))
        now = _now()
        if (
            issued_at - CLOCK_SKEW > now
            or expires_at <= issued_at
            or expires_at <= now
            or expires_at - issued_at
            > (MAX_AUTOMATED_E2E_LIFETIME if schema_version == 2 else MAX_LIFETIME)
        ):
            return _error(422, "validation_receipt_time_invalid")
        receipt_nonce = receipt.get("nonce")
        if (
            not isinstance(receipt_nonce, str)
            or _SAFE_NONCE.fullmatch(receipt_nonce) is None
        ):
            return _error(422, "validation_receipt_nonce_invalid")
        receipt_sha = hashlib.sha256(_canonical_manifest(receipt)).hexdigest()
        _verify_signature(self._hass, receipt, prefix="validation-")
        if schema_version == 2:
            operation_id = receipt["operation_id"]
        else:
            requested_operation_id = body.get("operation_id")
            if not _is_uuid_operation_id(requested_operation_id):
                return _error(422, "operation_id_uuid_required")
            operation_id = requested_operation_id
        request_sha = hashlib.sha256(
            _json_bytes(
                {
                    "action": "promote_home_command",
                    "request": body,
                }
            )
        ).hexdigest()
        operations = self._state.journal.setdefault("operations", {})
        existing_operation = operations.get(operation_id)
        if operation_id in operations or self._state.tx(operation_id) is not None:
            if (
                not isinstance(existing_operation, dict)
                or existing_operation.get("operation_id") != operation_id
                or existing_operation.get("action") != "promote_home_command"
                or existing_operation.get("request_sha256") != request_sha
                or existing_operation.get("receipt_sha256") != receipt_sha
                or existing_operation.get("transaction_id")
                != transaction.get("transaction_id")
                or existing_operation.get("preview_revision") != revision
                or existing_operation.get("expected_production_revision")
                != expected_production_revision
                or existing_operation.get("expected_production_config_sha256")
                != receipt.get("expected_production_config_sha256")
            ):
                return _error(409, "operation_id_conflict")
            status = existing_operation.get("status")
            if status == "committed":
                if (
                    self._state.journal.get("production_revision") != revision
                    or production_config_sha
                    != existing_operation.get("production_config_sha256")
                    or production_config_sha != preview_config_sha
                ):
                    return _error(409, "operation_readback_mismatch")
                try:
                    resource_binding = await asyncio.to_thread(
                        _verify_operation_resource_binding,
                        self._hass,
                        self._state,
                        existing_operation,
                        production_config,
                    )
                except (OSError, ValueError, RuntimeError):
                    return _error(409, "operation_readback_mismatch")
                return _json_response(
                    {
                        "promoted": True,
                        "operation_id": operation_id,
                        "status": "committed",
                        "active_revision": revision,
                        "previous_revision": existing_operation.get(
                            "previous_production_revision"
                        ),
                        "preview_config_sha256": preview_config_sha,
                        "expected_production_config_sha256": receipt.get(
                            "expected_production_config_sha256"
                        ),
                        "applied": True,
                        "verified": True,
                        "dashboard_resource_present": True,
                        "idempotent": True,
                        **resource_binding,
                    }
                )
            if status == "aborted":
                if (
                    self._state.journal.get("production_revision")
                    != expected_production_revision
                    or production_config_sha
                    != receipt.get("expected_production_config_sha256")
                ):
                    return _error(409, "operation_readback_mismatch")
                try:
                    resource_binding = await asyncio.to_thread(
                        _verify_recorded_resource_binding,
                        self._hass,
                        production_config,
                        existing_operation,
                        prefix="expected_",
                    )
                except (OSError, ValueError, RuntimeError):
                    return _error(409, "operation_readback_mismatch")
                return _json_response(
                    {
                        "promoted": False,
                        "operation_id": operation_id,
                        "status": "aborted",
                        "active_revision": expected_production_revision,
                        "expected_production_config_sha256": production_config_sha,
                        "applied": False,
                        "verified": True,
                        "dashboard_resource_present": bool(resource_binding),
                        "idempotent": True,
                        **resource_binding,
                    }
                )
            return _error(409, "operation_transition_in_progress")
        used_receipts = self._state.journal.setdefault("receipt_nonces", {})
        if receipt_nonce in used_receipts:
            return _error(409, "validation_receipt_replay")
        if any(
            receipt.get(key) != transaction.get(key)
            for key in ("manifest_sha256", "package_sha256", "dashboard_sha256")
        ):
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
            or receipt.get("expected_production_config_sha256") != production_config_sha
        ):
            return _error(409, "production_config_conflict")
        try:
            expected_resource_binding = await asyncio.to_thread(
                _dashboard_resource_binding_from_config,
                self._hass,
                production_config,
            )
        except (OSError, ValueError, RuntimeError):
            return _error(409, "production_config_integrity_failed")
        expected_resource_record = {
            "expected_" + key: value
            for key, value in expected_resource_binding.items()
        }
        transition = self._state.journal.get("production_transition")
        if isinstance(transition, dict) and transition.get("status") == "prepared":
            return _error(409, "production_transition_in_progress")
        previous = {
            "revision": expected_revision,
            "config": json.loads(_json_bytes(production_config)),
            "config_sha256": production_config_sha,
        }
        next_config = json.loads(_json_bytes(preview_config))
        prepared_at = _now().isoformat()
        operation_record = {
            "operation_id": operation_id,
            "action": "promote_home_command",
            "status": "prepared",
            "transaction_id": transaction["transaction_id"],
            "preview_revision": revision,
            "target_revision": revision,
            "expected_production_revision": expected_revision,
            "expected_production_config_sha256": production_config_sha,
            "preview_config_sha256": preview_config_sha,
            "previous_production_revision": expected_revision,
            "receipt_sha256": receipt_sha,
            "request_sha256": request_sha,
            "dashboard_resource_url": resource_context["preview_resource_url"],
            "dashboard_sha256": resource_context["preview_resource_sha256"],
            "dashboard_size": resource_context["preview_resource_size"],
            **expected_resource_record,
            "created_at": prepared_at,
        }
        transition = {
            "transition_id": "promotion-" + secrets.token_hex(16),
            "operation_id": operation_id,
            "status": "prepared",
            "expected_revision": expected_revision,
            "expected_config_sha256": production_config_sha,
            "to_revision": revision,
            "transaction_id": transaction["transaction_id"],
            "receipt_nonce": receipt_nonce,
            "receipt_sha256": receipt_sha,
            "request_sha256": request_sha,
            "previous": previous,
            "next_config": next_config,
            "next_config_sha256": preview_config_sha,
            "dashboard_resource_url": resource_context["preview_resource_url"],
            "dashboard_sha256": resource_context["preview_resource_sha256"],
            "dashboard_size": resource_context["preview_resource_size"],
            **expected_resource_record,
            "prepared_at": prepared_at,
        }
        if operation_id in operations or self._state.tx(operation_id) is not None:
            return _error(409, "operation_id_conflict")
        operations[operation_id] = operation_record
        used_receipts[receipt_nonce] = transaction["transaction_id"]
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
        _production_readback, production_readback = await _load_dashboard(
            self._hass, PRODUCTION
        )
        if _config_sha256(production_readback) != preview_config_sha:
            return _error(409, "production_readback_failed")
        try:
            await asyncio.to_thread(
                _verify_operation_resource_binding,
                self._hass,
                self._state,
                operation_record,
                production_readback,
            )
        except (OSError, ValueError, RuntimeError):
            return _error(409, "production_readback_failed")
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
        transaction["promotion_operation_id"] = operation_id
        transition["status"] = "committed"
        transition["committed_at"] = transaction["promoted_at"]
        operation_record["status"] = "committed"
        operation_record["production_config_sha256"] = preview_config_sha
        operation_record["completed_at"] = transaction["promoted_at"]
        self._state.save()
        return _json_response(
            {
                "promoted": True,
                "operation_id": operation_id,
                "status": "committed",
                "active_revision": revision,
                "previous_revision": previous.get("revision"),
                "preview_config_sha256": preview_config_sha,
                "expected_production_config_sha256": production_config_sha,
                "dashboard_resource_url": operation_record[
                    "dashboard_resource_url"
                ],
                "dashboard_sha256": operation_record["dashboard_sha256"],
                "dashboard_size": operation_record["dashboard_size"],
                "applied": True,
                "verified": True,
                "dashboard_resource_present": True,
            }
        )

    async def _rollback(  # noqa: C901 - explicit rollback CAS state machine
        self, body: dict[str, Any]
    ) -> web.Response:
        operation_id = body.get("operation_id")
        if set(body) != {
            "operation_id",
            "expected_current_revision",
            "expected_current_config_sha256",
        }:
            if isinstance(operation_id, str) and operation_id in (
                self._state.journal.get("operations", {})
            ):
                return _error(409, "operation_id_conflict")
            return _error(422, "rollback_request_schema_invalid")
        expected_current_revision = body.get("expected_current_revision")
        expected_current_config_sha = body.get("expected_current_config_sha256")
        if (
            not isinstance(operation_id, str)
            or _SAFE_NONCE.fullmatch(operation_id) is None
            or not isinstance(expected_current_revision, str)
            or not _is_sha256(expected_current_config_sha)
        ):
            return _error(422, "rollback_cas_required")
        request_sha = hashlib.sha256(
            _json_bytes({"action": "rollback_home_command", "request": body})
        ).hexdigest()
        operations = self._state.journal.setdefault("operations", {})
        if operation_id in operations or self._state.tx(operation_id) is not None:
            if operation_id not in operations:
                return _error(409, "operation_id_conflict")
            existing = operations[operation_id]
            if (
                isinstance(existing, dict)
                and existing.get("action") == "rollback_home_command"
                and existing.get("request_sha256") == request_sha
                and existing.get("status") == "committed"
            ):
                _production, current = await _load_dashboard(self._hass, PRODUCTION)
                current_sha = _config_sha256(current)
                if self._state.journal.get("production_revision") != existing.get(
                    "target_revision"
                ) or current_sha != existing.get("production_config_sha256"):
                    return _error(409, "operation_readback_mismatch")
                try:
                    resource_binding = await asyncio.to_thread(
                        _verify_recorded_resource_binding,
                        self._hass,
                        current,
                        existing,
                    )
                except (OSError, ValueError, RuntimeError):
                    return _error(409, "operation_readback_mismatch")
                return _json_response(
                    {
                        "rolled_back": True,
                        "operation_id": operation_id,
                        "status": "committed",
                        "active_revision": existing.get("target_revision"),
                        "production_config_sha256": current_sha,
                        "applied": True,
                        "verified": True,
                        "dashboard_resource_present": bool(resource_binding),
                        "idempotent": True,
                        **resource_binding,
                    }
                )
            if (
                isinstance(existing, dict)
                and existing.get("action") == "rollback_home_command"
                and existing.get("request_sha256") == request_sha
                and existing.get("status") == "aborted"
            ):
                _production, current = await _load_dashboard(self._hass, PRODUCTION)
                current_sha = _config_sha256(current)
                if (
                    self._state.journal.get("production_revision")
                    != existing.get("expected_production_revision")
                    or current_sha
                    != existing.get("expected_production_config_sha256")
                ):
                    return _error(409, "operation_readback_mismatch")
                try:
                    resource_binding = await asyncio.to_thread(
                        _verify_recorded_resource_binding,
                        self._hass,
                        current,
                        existing,
                        prefix="expected_",
                    )
                except (OSError, ValueError, RuntimeError):
                    return _error(409, "operation_readback_mismatch")
                return _json_response(
                    {
                        "rolled_back": False,
                        "operation_id": operation_id,
                        "status": "aborted",
                        "active_revision": existing.get(
                            "expected_production_revision"
                        ),
                        "production_config_sha256": current_sha,
                        "applied": False,
                        "verified": True,
                        "dashboard_resource_present": bool(resource_binding),
                        "idempotent": True,
                        **resource_binding,
                    }
                )
            return _error(409, "operation_id_conflict")
        previous = self._state.journal.get("previous_production")
        if (
            not isinstance(previous, dict)
            or not isinstance(previous.get("revision"), str)
            or not isinstance(previous.get("config"), dict)
            or not _is_sha256(previous.get("config_sha256"))
            or not _is_sha256(self._state.journal.get("production_config_sha256"))
        ):
            return _error(409, "no_prior_production_revision")
        active = self._state.journal.get("production_revision")
        if expected_current_revision != active:
            return _error(409, "production_revision_conflict")
        if expected_current_config_sha != self._state.journal.get(
            "production_config_sha256"
        ):
            return _error(409, "production_config_conflict")
        production, current = await _load_dashboard(self._hass, PRODUCTION)
        if _config_sha256(current) != expected_current_config_sha:
            return _error(409, "production_config_conflict")
        if _config_sha256(previous["config"]) != previous["config_sha256"]:
            return _error(409, "production_rollback_evidence_invalid")
        try:
            expected_resource_binding = await asyncio.to_thread(
                _dashboard_resource_binding_from_config, self._hass, current
            )
            target_resource_binding = await asyncio.to_thread(
                _dashboard_resource_binding_from_config,
                self._hass,
                previous["config"],
            )
        except (OSError, ValueError, RuntimeError):
            return _error(409, "production_rollback_evidence_invalid")
        expected_resource_record = {
            "expected_" + key: value
            for key, value in expected_resource_binding.items()
        }
        prepared_at = _now().isoformat()
        operation_record = {
            "operation_id": operation_id,
            "action": "rollback_home_command",
            "status": "prepared",
            "expected_production_revision": active,
            "expected_production_config_sha256": expected_current_config_sha,
            "target_revision": previous["revision"],
            "request_sha256": request_sha,
            **target_resource_binding,
            **expected_resource_record,
            "created_at": prepared_at,
        }
        transition = {
            "operation_id": operation_id,
            "status": "prepared",
            "from_revision": active,
            "to_revision": previous["revision"],
            "expected_config_sha256": expected_current_config_sha,
            "next_config": json.loads(_json_bytes(previous["config"])),
            "next_config_sha256": previous["config_sha256"],
            "request_sha256": request_sha,
            **target_resource_binding,
            **expected_resource_record,
            "prepared_at": prepared_at,
        }
        operations[operation_id] = operation_record
        self._state.journal["rollback_transition"] = transition
        self._state.save()
        if (
            self._state.journal.get("production_revision") != active
            or self._state.journal.get("production_config_sha256")
            != expected_current_config_sha
            or self._state.journal.get("rollback_transition") is not transition
        ):
            return _error(409, "production_revision_conflict")
        await production.async_save(previous["config"])
        _production_readback, readback = await _load_dashboard(self._hass, PRODUCTION)
        if _config_sha256(readback) != previous["config_sha256"]:
            return _error(409, "production_rollback_readback_failed")
        try:
            await asyncio.to_thread(
                _verify_recorded_resource_binding,
                self._hass,
                readback,
                operation_record,
            )
        except (OSError, ValueError, RuntimeError):
            return _error(409, "production_rollback_readback_failed")
        if (
            self._state.journal.get("production_revision") != active
            or self._state.journal.get("production_config_sha256")
            != expected_current_config_sha
        ):
            return _error(409, "production_revision_conflict")
        _commit_rollback_transition(self._state, transition, operation_record)
        return _json_response(
            {
                "rolled_back": True,
                "operation_id": operation_id,
                "status": "committed",
                "active_revision": previous["revision"],
                "production_config_sha256": previous["config_sha256"],
                "applied": True,
                "verified": True,
                "dashboard_resource_present": bool(target_resource_binding),
                **target_resource_binding,
            }
        )


class TransactionView(HomeAssistantView):
    """Readback, activation, and reload routes for one transaction."""

    requires_auth = True
    url = BASE + "/{transaction_id}/{operation}"
    name = DOMAIN + ":transaction"

    def __init__(self, hass: HomeAssistant, state: AuroraState) -> None:
        self._hass = hass
        self._state = state

    async def get(  # noqa: C901 - transaction and operation readback dispatcher
        self, request: web.Request, transaction_id: str, operation: str
    ) -> web.Response:
        if not await _admin(request):
            return _error(403, "admin_required")
        if not self._state.admit_request():
            return _error(429, "rate_limited")
        if operation in {"status", "readback"}:
            async with self._state.lock:
                operation_record = self._state.journal.get("operations", {}).get(
                    transaction_id
                )
                if isinstance(operation_record, dict):
                    try:
                        await _reconcile_operation_transition(
                            self._hass,
                            self._state,
                            transaction_id,
                            operation_record,
                        )
                    except (OSError, ValueError, RuntimeError):
                        return _error(409, "production_recovery_required")
                    operation_record = self._state.journal.get("operations", {}).get(
                        transaction_id
                    )
                    if not isinstance(operation_record, dict):
                        return _error(409, "operation_recovery_invalid")
                    public_keys = (
                        "operation_id",
                        "action",
                        "status",
                        "transaction_id",
                        "preview_revision",
                        "target_revision",
                        "expected_production_revision",
                        "expected_production_config_sha256",
                        "preview_config_sha256",
                        "production_config_sha256",
                        "created_at",
                        "completed_at",
                    )
                    payload = {
                        key: operation_record.get(key)
                        for key in public_keys
                        if operation_record.get(key) is not None
                    }
                    if operation_record.get("action") == "rollback_home_command":
                        payload.update(
                            {
                                "expected_current_revision": operation_record.get(
                                    "expected_production_revision"
                                ),
                                "expected_current_config_sha256": operation_record.get(
                                    "expected_production_config_sha256"
                                ),
                            }
                        )
                    if operation == "readback":
                        try:
                            _production, production_config = await _load_dashboard(
                                self._hass, PRODUCTION
                            )
                        except (OSError, ValueError, RuntimeError):
                            return _error(409, "operation_readback_failed")
                        live_sha = _config_sha256(production_config)
                        status = operation_record.get("status")
                        if status == "committed":
                            expected_revision = operation_record.get(
                                "target_revision",
                                operation_record.get("preview_revision"),
                            )
                            expected_sha = operation_record.get(
                                "production_config_sha256"
                            )
                            applied = True
                        elif status == "aborted":
                            expected_revision = operation_record.get(
                                "expected_production_revision"
                            )
                            expected_sha = operation_record.get(
                                "expected_production_config_sha256"
                            )
                            applied = False
                        else:
                            return _error(409, "operation_readback_mismatch")
                        if (
                            self._state.journal.get("production_revision")
                            != expected_revision
                            or live_sha != expected_sha
                        ):
                            return _error(409, "operation_readback_mismatch")
                        resource_binding: dict[str, Any] = {}
                        if (
                            applied
                            and operation_record.get("action") == "promote_home_command"
                        ):
                            try:
                                resource_binding = await asyncio.to_thread(
                                    _verify_operation_resource_binding,
                                    self._hass,
                                    self._state,
                                    operation_record,
                                    production_config,
                                )
                            except (OSError, ValueError, RuntimeError):
                                return _error(409, "operation_readback_mismatch")
                        elif operation_record.get("action") == "promote_home_command":
                            try:
                                resource_binding = await asyncio.to_thread(
                                    _verify_recorded_resource_binding,
                                    self._hass,
                                    production_config,
                                    operation_record,
                                    prefix="expected_",
                                )
                            except (OSError, ValueError, RuntimeError):
                                return _error(409, "operation_readback_mismatch")
                        elif operation_record.get("action") == "rollback_home_command":
                            try:
                                resource_binding = await asyncio.to_thread(
                                    _verify_recorded_resource_binding,
                                    self._hass,
                                    production_config,
                                    operation_record,
                                    prefix="" if applied else "expected_",
                                )
                            except (OSError, ValueError, RuntimeError):
                                return _error(409, "operation_readback_mismatch")
                        payload.update(
                            {
                                "target_dashboard": PRODUCTION,
                                "active_revision": self._state.journal.get(
                                    "production_revision"
                                ),
                                "live_production_config_sha256": live_sha,
                                "applied": applied,
                                "verified": True,
                                "dashboard_resource_present": bool(resource_binding),
                                **resource_binding,
                            }
                        )
                    return _json_response(payload)
        if operation != "readback":
            return _error(404, "not_found")
        async with self._state.lock:
            transaction = self._state.tx(transaction_id)
            if transaction is None:
                return _error(404, "transaction_not_found")
            try:
                await _reconcile_transaction_transition(
                    self._hass, self._state, transaction
                )
            except (OSError, ValueError, RuntimeError):
                return _error(409, "transaction_recovery_required")
            return await self._transaction_readback(transaction)

    async def _transaction_readback(  # noqa: C901 - fail-closed outcome projection
        self, transaction: dict[str, Any]
    ) -> web.Response:
        stage = transaction.get("stage_transition")
        previous_revision = (
            transaction.get("preview_revision_before")
            if "preview_revision_before" in transaction
            else transaction.get("stage_previous_revision")
        )
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
        payload["previous_revision"] = previous_revision
        payload["dashboard_resource_present"] = False
        if isinstance(stage, dict) and stage.get("status") in {
            "committed",
            "aborted",
        }:
            payload["stage_status"] = stage["status"]
        if transaction.get("status") == "aborted":
            if (
                not isinstance(stage, dict)
                or stage.get("status") != "aborted"
                or stage.get("transaction_id") != transaction.get("transaction_id")
                or stage.get("request_sha256")
                != transaction.get("stage_request_sha256")
            ):
                return _error(409, "stage_readback_failed")
            payload.update({"verified": False, "staged_package_verified": False})
            return _json_response(payload)
        if any(
            (
                transaction.get("target") != PREVIEW,
                not _is_sha256(transaction.get("manifest_sha256")),
                not _is_sha256(transaction.get("package_sha256")),
                not _is_sha256(transaction.get("dashboard_sha256")),
                transaction.get("status") == "staging",
            )
        ):
            return _error(409, "staged_integrity_failed")
        try:
            package, dashboard, manifest = _verify_staged_artifacts(
                transaction, self._state.root
            )
        except (OSError, ValueError):
            return _error(409, "staged_integrity_failed")
        if package is None or dashboard is None or manifest is None:
            return _error(409, "staged_integrity_failed")
        active_dashboard_sha: str | None = None
        active_resource_binding: dict[str, Any] = {}
        if transaction.get("status") in {"activated", "reloaded", "promoted"}:
            try:
                _validate_package(package)
                active_dashboard_sha = await _verify_active_bindings(
                    self._hass, transaction, dashboard
                )
                active_context = await asyncio.to_thread(
                    _active_resource_context, self._hass, transaction
                )
                active_resource_binding = {
                    "active_dashboard_resource_url": active_context[
                        "preview_resource_url"
                    ],
                    "active_dashboard_sha256": active_context[
                        "preview_resource_sha256"
                    ],
                    "active_dashboard_size": active_context[
                        "preview_resource_size"
                    ],
                }
                _preview, preview_config = await _load_dashboard(self._hass, PREVIEW)
                if any(
                    (
                        self._state.journal.get("active_preview")
                        != transaction.get("revision"),
                        not _is_sha256(
                            transaction.get("active_preview_config_sha256")
                        ),
                        _config_sha256(preview_config)
                        != transaction.get("active_preview_config_sha256"),
                    )
                ):
                    raise ValueError("active_preview_binding")
            except (OSError, ValueError, RuntimeError):
                return _error(409, "active_integrity_failed")
        payload["staged_package_verified"] = True
        activation = transaction.get("activation_transition")
        if isinstance(activation, dict) and activation.get("status") in {
            "committed",
            "aborted",
        }:
            payload["activation_status"] = activation["status"]
        rollback = transaction.get("preview_rollback_transition")
        if isinstance(rollback, dict) and rollback.get("status") in {
            "committed",
            "aborted",
        }:
            payload["rollback_status"] = rollback["status"]
        if active_dashboard_sha is not None:
            payload.update(
                {
                    "active_revision": self._state.journal.get("active_preview"),
                    "active_dashboard_verified": True,
                    "dashboard_resource_present": True,
                    **active_resource_binding,
                }
            )
        if transaction.get("status") == "rolled_back":
            if (
                not isinstance(rollback, dict)
                or rollback.get("status") != "committed"
                or rollback.get("action") != "rollback"
                or rollback.get("transaction_id")
                != transaction.get("transaction_id")
                or rollback.get("from_revision") != transaction.get("revision")
                or rollback.get("to_revision")
                != transaction.get("preview_revision_before")
                or rollback.get("next_config_sha256")
                != transaction.get("preview_config_sha256_before")
                or not _is_sha256(rollback.get("next_config_sha256"))
            ):
                return _error(409, "preview_rollback_readback_failed")
            try:
                _preview, preview_config = await _load_dashboard(self._hass, PREVIEW)
            except (OSError, ValueError, RuntimeError):
                return _error(409, "preview_rollback_readback_failed")
            active_revision = self._state.journal.get("active_preview")
            if (
                _config_sha256(preview_config) != rollback["next_config_sha256"]
                or active_revision != rollback.get("to_revision")
            ):
                return _error(409, "preview_rollback_readback_failed")
            try:
                restored_resource_binding = await asyncio.to_thread(
                    _verify_preview_revision_resource_binding,
                    self._hass,
                    self._state,
                    active_revision,
                    preview_config,
                )
            except (OSError, ValueError, RuntimeError):
                return _error(409, "preview_rollback_readback_failed")
            payload.update(
                {
                    "rolled_back": True,
                    "active_revision": active_revision,
                    "previous_revision": active_revision,
                    "preview_active": isinstance(active_revision, str),
                    "dashboard_resource_present": bool(restored_resource_binding),
                    "active_dashboard_verified": bool(restored_resource_binding),
                    **(
                        {
                            "active_dashboard_resource_url": restored_resource_binding[
                                "dashboard_resource_url"
                            ],
                            "active_dashboard_sha256": restored_resource_binding[
                                "dashboard_sha256"
                            ],
                            "active_dashboard_size": restored_resource_binding[
                                "dashboard_size"
                            ],
                        }
                        if restored_resource_binding
                        else {}
                    ),
                }
            )
        payload["verified"] = transaction.get("status") in {
            "verified",
            "activated",
            "reloaded",
            "promoted",
            "rolled_back",
        }
        return _json_response(payload)

    async def post(  # noqa: C901 - explicit transaction lifecycle dispatcher
        self, request: web.Request, transaction_id: str, operation: str
    ) -> web.Response:
        if not await _admin(request):
            return _error(403, "admin_required")
        if not self._state.admit_request():
            return _error(429, "rate_limited")
        async with self._state.lock:
            transaction = self._state.tx(transaction_id)
            if transaction is None:
                return _error(404, "transaction_not_found")
            try:
                await _reconcile_transaction_transition(
                    self._hass, self._state, transaction
                )
                if operation == "activate":
                    if transaction.get("status") in {
                        "activated",
                        "reloaded",
                        "promoted",
                    } and self._state.journal.get("active_preview") == transaction.get(
                        "revision"
                    ):
                        await _verify_active_transaction(
                            self._hass, transaction, self._state.root
                        )
                        _preview, active_config = await _load_dashboard(
                            self._hass, PREVIEW
                        )
                        if (
                            not _is_sha256(
                                transaction.get("active_preview_config_sha256")
                            )
                            or _config_sha256(active_config)
                            != transaction.get("active_preview_config_sha256")
                        ):
                            return _error(409, "active_integrity_failed")
                        return _json_response(
                            {
                                "activated": True,
                                "active_revision": transaction["revision"],
                                "previous_revision": transaction.get(
                                    "preview_revision_before"
                                ),
                                "status": transaction["status"],
                                "restart_required": False,
                                "backend_unchanged": True,
                                "idempotent": True,
                            }
                        )
                    if transaction.get("status") != "verified":
                        return _error(409, "transaction_not_verified")
                    package, dashboard, _manifest = _verify_staged_artifacts(
                        transaction, self._state.root
                    )
                    if package is None or dashboard is None:
                        raise ValueError("staged_artifact_missing")
                    _validate_package(package)
                    await _ensure_preview(self._hass)
                    preview, preview_config = await _load_dashboard(self._hass, PREVIEW)
                    try:
                        await asyncio.to_thread(
                            _verify_preview_revision_resource_binding,
                            self._hass,
                            self._state,
                            self._state.journal.get("active_preview"),
                            preview_config,
                        )
                    except (OSError, ValueError, RuntimeError):
                        return _error(409, "preview_prestate_invalid")
                    transaction["preview_before"] = json.loads(
                        _json_bytes(preview_config)
                    )
                    transaction["preview_config_sha256_before"] = _config_sha256(
                        preview_config
                    )
                    transaction["preview_revision_before"] = self._state.journal.get(
                        "active_preview"
                    )
                    asset_name = (
                        f"aurora-preview-dashboard-{transaction['dashboard_sha256']}.js"
                    )
                    next_config = _dashboard_config_with_asset(
                        preview_config, asset_name
                    )
                    transition = {
                        "status": "prepared",
                        "action": "activate",
                        "transaction_id": transaction["transaction_id"],
                        "previous_revision": transaction.get(
                            "preview_revision_before"
                        ),
                        "next_revision": transaction["revision"],
                        "previous_config_sha256": _config_sha256(preview_config),
                        "next_config_sha256": _config_sha256(next_config),
                        "asset_name": asset_name,
                        "prepared_at": _now().isoformat(),
                    }
                    transaction["active_dashboard_asset"] = asset_name
                    transaction["activation_transition"] = transition
                    self._state.save()
                    try:
                        saved_asset_name = await _save_preview_asset(
                            self._hass, dashboard
                        )
                        if saved_asset_name != asset_name:
                            raise ValueError("active_dashboard_binding")
                    except (OSError, ValueError, RuntimeError):
                        try:
                            await preview.async_save(transaction["preview_before"])
                        except (OSError, ValueError, RuntimeError):
                            pass
                        raise
                    try:
                        _preview_readback, preview_readback = await _load_dashboard(
                            self._hass, PREVIEW
                        )
                        await _verify_active_bindings(
                            self._hass, transaction, dashboard
                        )
                    except (OSError, ValueError, RuntimeError):
                        try:
                            await preview.async_save(transaction["preview_before"])
                        except (OSError, ValueError, RuntimeError):
                            pass
                        raise
                    if (
                        _config_sha256(preview_readback)
                        != transition["next_config_sha256"]
                    ):
                        raise ValueError("preview_activation_readback_failed")
                    _commit_preview_activation(self._state, transaction, transition)
                    return _json_response(
                        {
                            "activated": True,
                            "active_revision": transaction["revision"],
                            "previous_revision": transaction.get(
                                "preview_revision_before"
                            ),
                            "status": "activated",
                            "restart_required": False,
                            "backend_unchanged": True,
                        }
                    )
                if operation == "rollback":
                    if transaction.get("status") == "rolled_back":
                        rollback = transaction.get("preview_rollback_transition")
                        active_revision = self._state.journal.get("active_preview")
                        try:
                            _preview, rolled_back_config = await _load_dashboard(
                                self._hass, PREVIEW
                            )
                        except (OSError, ValueError, RuntimeError):
                            return _error(409, "preview_rollback_readback_failed")
                        if (
                            not isinstance(rollback, dict)
                            or rollback.get("status") != "committed"
                            or rollback.get("action") != "rollback"
                            or rollback.get("transaction_id")
                            != transaction.get("transaction_id")
                            or rollback.get("from_revision")
                            != transaction.get("revision")
                            or rollback.get("to_revision") != active_revision
                            or rollback.get("to_revision")
                            != transaction.get("preview_revision_before")
                            or not _is_sha256(rollback.get("next_config_sha256"))
                            or rollback.get("next_config_sha256")
                            != transaction.get("preview_config_sha256_before")
                            or not isinstance(transaction.get("preview_before"), dict)
                            or _config_sha256(transaction["preview_before"])
                            != rollback.get("next_config_sha256")
                            or _config_sha256(rolled_back_config)
                            != rollback.get("next_config_sha256")
                        ):
                            return _error(409, "preview_rollback_readback_failed")
                        try:
                            await asyncio.to_thread(
                                _verify_preview_revision_resource_binding,
                                self._hass,
                                self._state,
                                active_revision,
                                rolled_back_config,
                            )
                        except (OSError, ValueError, RuntimeError):
                            return _error(409, "preview_rollback_readback_failed")
                        return _json_response(
                            {
                                "rolled_back": True,
                                "active_revision": active_revision,
                                "previous_revision": active_revision,
                                "preview_active": isinstance(active_revision, str),
                                "status": "rolled_back",
                                "idempotent": True,
                            }
                        )
                    if transaction.get("status") not in {"activated", "reloaded"}:
                        return _error(409, "transaction_not_rollbackable")
                    previous_preview = transaction.get("preview_before")
                    if (
                        not isinstance(previous_preview, dict)
                        or not _is_sha256(
                            transaction.get("preview_config_sha256_before")
                        )
                        or _config_sha256(previous_preview)
                        != transaction.get("preview_config_sha256_before")
                        or not _is_sha256(
                            transaction.get("active_preview_config_sha256")
                        )
                    ):
                        return _error(409, "preview_rollback_snapshot_missing")
                    try:
                        await asyncio.to_thread(
                            _verify_preview_revision_resource_binding,
                            self._hass,
                            self._state,
                            transaction.get("preview_revision_before"),
                            previous_preview,
                        )
                    except (OSError, ValueError, RuntimeError):
                        return _error(409, "preview_rollback_snapshot_missing")
                    if self._state.journal.get("active_preview") != transaction.get(
                        "revision"
                    ):
                        return _error(409, "preview_revision_conflict")
                    preview, current = await _load_dashboard(self._hass, PREVIEW)
                    if _config_sha256(current) != transaction.get(
                        "active_preview_config_sha256"
                    ):
                        return _error(409, "preview_config_conflict")
                    try:
                        await _verify_active_transaction(
                            self._hass, transaction, self._state.root
                        )
                    except (OSError, ValueError, RuntimeError):
                        return _error(409, "active_integrity_failed")
                    transition = {
                        "status": "prepared",
                        "action": "rollback",
                        "transaction_id": transaction["transaction_id"],
                        "from_status": transaction["status"],
                        "from_revision": transaction["revision"],
                        "to_revision": transaction.get("preview_revision_before"),
                        "previous_config_sha256": _config_sha256(current),
                        "next_config_sha256": transaction[
                            "preview_config_sha256_before"
                        ],
                        "asset_name": transaction.get("active_dashboard_asset"),
                        "prepared_at": _now().isoformat(),
                    }
                    transaction["preview_rollback_transition"] = transition
                    self._state.save()
                    await preview.async_save(previous_preview)
                    _preview_readback, preview_readback = await _load_dashboard(
                        self._hass, PREVIEW
                    )
                    if _config_sha256(preview_readback) != transaction.get(
                        "preview_config_sha256_before"
                    ):
                        return _error(409, "preview_rollback_readback_failed")
                    try:
                        await asyncio.to_thread(
                            _verify_preview_revision_resource_binding,
                            self._hass,
                            self._state,
                            transaction.get("preview_revision_before"),
                            preview_readback,
                        )
                    except (OSError, ValueError, RuntimeError):
                        return _error(409, "preview_rollback_readback_failed")
                    _commit_preview_rollback(self._state, transaction, transition)
                    active_revision = self._state.journal.get("active_preview")
                    return _json_response(
                        {
                            "rolled_back": True,
                            "active_revision": active_revision,
                            "previous_revision": active_revision,
                            "preview_active": isinstance(active_revision, str),
                            "status": "rolled_back",
                        }
                    )
                if operation == "reload":
                    if transaction.get("status") == "reloaded":
                        await _verify_active_transaction(
                            self._hass, transaction, self._state.root
                        )
                        _preview, active_config = await _load_dashboard(
                            self._hass, PREVIEW
                        )
                        if (
                            not _is_sha256(
                                transaction.get("active_preview_config_sha256")
                            )
                            or _config_sha256(active_config)
                            != transaction.get("active_preview_config_sha256")
                        ):
                            return _error(409, "active_integrity_failed")
                        return _json_response(
                            {
                                "reloaded": True,
                                "verified": True,
                                "active_revision": transaction["revision"],
                                "previous_revision": transaction.get(
                                    "preview_revision_before"
                                ),
                                "status": "reloaded",
                                "restart_required": False,
                                "backend_unchanged": True,
                                "idempotent": True,
                            }
                        )
                    if transaction.get("status") != "activated":
                        return _error(409, "transaction_not_activated")
                    await _verify_active_transaction(
                        self._hass, transaction, self._state.root
                    )
                    _preview, active_config = await _load_dashboard(self._hass, PREVIEW)
                    if (
                        not _is_sha256(
                            transaction.get("active_preview_config_sha256")
                        )
                        or _config_sha256(active_config)
                        != transaction.get("active_preview_config_sha256")
                    ):
                        return _error(409, "active_integrity_failed")
                    transaction["status"] = "reloaded"
                    transaction["reloaded_at"] = _now().isoformat()
                    self._state.save()
                    return _json_response(
                        {
                            "reloaded": True,
                            "verified": True,
                            "active_revision": transaction["revision"],
                            "previous_revision": transaction.get(
                                "preview_revision_before"
                            ),
                            "status": "reloaded",
                            "restart_required": False,
                            "backend_unchanged": True,
                        }
                    )
            except (OSError, ValueError, RuntimeError):
                return _error(500, "activation_failed")
        return _error(404, "not_found")
