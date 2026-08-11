"""TDD coverage for the fixed-scope Aurora deployment adapter."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.aurora_deploy import adapter
from custom_components.aurora_deploy.adapter import (
    _canonical_manifest,
    _validate_manifest,
    _validate_package,
)


def _package(*, name: str | None = None, content: bytes = b"{}") -> bytes:
    if name is None and content != b"{}":
        name = adapter.PACKAGE_ROOT + "manifest.json"
    component_files = {
        filename: (
            json.dumps(
                {"domain": adapter.COMPONENT_DOMAIN, "version": adapter.COMPONENT_VERSION}
            ).encode()
            if filename == "manifest.json"
            else f"safe-{filename}".encode()
        )
        for filename in adapter.APPROVED_COMPONENT_FILES
    }
    if name is not None and name.startswith(adapter.PACKAGE_ROOT):
        component_files[name.removeprefix(adapter.PACKAGE_ROOT)] = content
    component_entries = [
        {
            "path": adapter.PACKAGE_ROOT + filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        for filename, data in sorted(component_files.items())
    ]
    component_manifest = json.dumps(
        {
            "schemaVersion": "1.0",
            "domain": adapter.COMPONENT_DOMAIN,
            "version": adapter.COMPONENT_VERSION,
            "configurationKey": adapter.COMPONENT_DOMAIN,
            "restartRequired": True,
            "files": component_entries,
            "installation": {
                "mode": "transactional-atomic-rename",
                "installer": "install-aurora-camera-ai-component.py",
                "configurationHashGuardRequired": True,
                "prestateCapture": "exact-bytes",
            },
            "rollback": {
                "mode": "restore-exact-prestate",
                "configurationHashGuardRequired": True,
                "restartRequired": True,
            },
        },
        sort_keys=True,
    ).encode()
    activation = b'{"safe":true}'
    installer = b"safe-installer"
    package_manifest = json.dumps(
        {
            "entry": "activation-manifest.json",
            "sha256": hashlib.sha256(activation).hexdigest(),
            "componentManifestEntry": "custom-component-manifest.json",
            "componentManifestSha256": hashlib.sha256(component_manifest).hexdigest(),
            "installer": {
                "entry": "install-aurora-camera-ai-component.py",
                "sha256": hashlib.sha256(installer).hexdigest(),
                "size": len(installer),
            },
        },
        sort_keys=True,
    ).encode()
    files = {
        "manifest.json": package_manifest,
        "activation-manifest.json": activation,
        "custom-component-manifest.json": component_manifest,
        "install-aurora-camera-ai-component.py": installer,
        **{
            adapter.PACKAGE_ROOT + filename: data
            for filename, data in component_files.items()
        },
    }
    if name is not None and not name.startswith(adapter.PACKAGE_ROOT):
        files[name] = content
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for filename, data in files.items():
            item = tarfile.TarInfo(filename)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def _hass(tmp_path: Path, public_key: bytes):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "aurora_deploy_trusted_keys.json").write_text(
        json.dumps({"release-test": base64.b64encode(public_key).decode()}),
        encoding="utf-8",
    )
    return SimpleNamespace(config=SimpleNamespace(path=lambda value: str(config_dir / value)))


def _manifest(private_key, package: bytes, dashboard: bytes, *, expires: datetime | None = None) -> dict:
    now = datetime.now(UTC)
    document = {
        "schema_version": 1,
        "target": "aurora-v9-preview",
        "dashboard_target": "aurora-preview",
        "preview_only": True,
        "target_release": "0.1.16",
        "privacy_policy": "no-sensitive-inference-v1",
        "key_id": "release-test",
        "signer": "release-test",
        "issued_at": now.isoformat(),
        "expires_at": (expires or now + timedelta(hours=1)).isoformat(),
        "nonce": "nonce-test-123",
        "artifact_sha256": hashlib.sha256(package).hexdigest(),
        "dashboard_sha256": hashlib.sha256(dashboard).hexdigest(),
        "assets": [
            {"name": "aurora-preview-package", "sha256": hashlib.sha256(package).hexdigest()},
            {"name": "aurora-preview-dashboard", "sha256": hashlib.sha256(dashboard).hexdigest()},
        ],
    }
    document["signature"] = base64.b64encode(private_key.sign(_canonical_manifest(document))).decode()
    return document


def test_signed_manifest_and_fixed_archive_are_accepted(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    package = _package()
    dashboard = b"export const AuroraPreview = true;"
    manifest = _manifest(private, package, dashboard)
    result = _validate_manifest(_hass(tmp_path, private.public_key().public_bytes_raw()), manifest, package, dashboard)
    assert result == (
        hashlib.sha256(_canonical_manifest(manifest)).hexdigest(),
        hashlib.sha256(package).hexdigest(),
        hashlib.sha256(dashboard).hexdigest(),
    )


def test_wrong_signature_and_expired_manifest_fail_closed(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    package = _package()
    dashboard = b"safe"
    expired = _manifest(private, package, dashboard, expires=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(ValueError, match="expiry"):
        _validate_manifest(_hass(tmp_path, private.public_key().public_bytes_raw()), expired, package, dashboard)
    expired["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    expired["signature"] = base64.b64encode(Ed25519PrivateKey.generate().sign(_canonical_manifest(expired))).decode()
    with pytest.raises(ValueError, match="signature"):
        _validate_manifest(_hass(tmp_path, private.public_key().public_bytes_raw()), expired, package, dashboard)


def test_manifest_requires_explicit_preview_and_privacy_binding(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    package = _package()
    dashboard = b"safe"
    manifest = _manifest(private, package, dashboard)
    for field, value, expected in (
        ("preview_only", False, "dashboard"),
        ("privacy_policy", "other-policy", "privacy_policy"),
    ):
        candidate = {**manifest, field: value}
        candidate["signature"] = base64.b64encode(
            private.sign(_canonical_manifest(candidate))
        ).decode()
        with pytest.raises(ValueError, match=expected):
            _validate_manifest(
                _hass(tmp_path, private.public_key().public_bytes_raw()),
                candidate,
                package,
                dashboard,
            )


def test_archive_rejects_traversal_links_nested_and_unapproved_members() -> None:
    for name in ("../escape", "custom_components/other/file.py", "nested.tgz"):
        with pytest.raises(ValueError):
            _validate_package(_package(name=name))

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        item = tarfile.TarInfo("custom_components/aurora_camera_ai/link.py")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        archive.addfile(item)
    with pytest.raises(ValueError, match="package_link"):
        _validate_package(output.getvalue())


def test_archive_accepts_declared_synthetic_fixture_but_rejects_other_extras() -> None:
    synthetic = b"safe-synthetic-fixture"

    reviewed = _validate_package(
        _package(
            name=adapter.PACKAGE_ROOT + "synthetic_fixture.py",
            content=synthetic,
        )
    )

    assert reviewed["synthetic_fixture.py"] == synthetic
    assert set(reviewed) == adapter.APPROVED_COMPONENT_FILES
    with pytest.raises(ValueError, match="package_member"):
        _validate_package(
            _package(
                name=adapter.PACKAGE_ROOT + "undeclared_fixture.py",
                content=b"undeclared",
            )
        )


def test_archive_requires_complete_reviewed_component_source() -> None:
    raw = _package()
    source = io.BytesIO(raw)
    output = io.BytesIO()
    missing = adapter.PACKAGE_ROOT + "coordinator.py"
    with (
        tarfile.open(fileobj=source, mode="r:gz") as original,
        tarfile.open(fileobj=output, mode="w:gz") as rebuilt,
    ):
        for member in original.getmembers():
            if member.name == missing:
                continue
            stream = original.extractfile(member)
            rebuilt.addfile(member, stream)
    with pytest.raises(ValueError, match="package_source_missing"):
        _validate_package(output.getvalue())


def test_privacy_functionality_is_rejected_before_staging() -> None:
    with pytest.raises(ValueError):
        _validate_package(_package(content=b"biometric identity inference"))

@pytest.mark.asyncio
async def test_setup_registers_only_authenticated_fixed_views(tmp_path: Path) -> None:
    from custom_components import aurora_deploy

    class Http:
        def __init__(self) -> None:
            self.views = []

        def register_view(self, view) -> None:
            self.views.append(view)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda value: str(config_dir / value)),
        http=Http(),
        data={},
    )

    async def executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = executor
    assert await aurora_deploy.async_setup(hass, {}) is True
    assert [view.url for view in hass.http.views] == [
        "/api/aurora/deploy-preview/v1/{operation}",
        "/api/aurora/deploy-preview/v1/{transaction_id}/{operation}",
    ]
    assert all(view.requires_auth for view in hass.http.views)
