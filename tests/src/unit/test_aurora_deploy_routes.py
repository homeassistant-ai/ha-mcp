"""Route-level tests for the fixed-scope Aurora deployment adapter."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.aurora_deploy import adapter


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, _limit: int) -> bytes:
        return self._raw


class _Request(dict):
    def __init__(self, body: dict | None = None, *, admin: bool = True) -> None:
        raw = json.dumps(body or {}).encode()
        super().__init__(hass_user=SimpleNamespace(is_admin=admin))
        self.content = _Content(raw)
        self.content_length = len(raw)


class _Dashboard:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.async_load = AsyncMock(return_value=config)
        self.async_save = AsyncMock()


class _Response:
    def __init__(self, payload: dict, status: int) -> None:
        self.status = status
        self.body = json.dumps(payload).encode()


@pytest.fixture(autouse=True)
def _standalone_web_response(monkeypatch):
    if not hasattr(adapter.web, "json_response"):
        monkeypatch.setattr(
            adapter.web,
            "json_response",
            lambda payload, status=200: _Response(payload, status),
            raising=False,
        )


def _hass(tmp_path):
    return SimpleNamespace(
        config=SimpleNamespace(path=lambda relative: str(tmp_path / relative)),
        data={},
    )


def _state(tmp_path, journal: dict | None = None):
    return adapter.AuroraState(_hass(tmp_path), tmp_path, journal or {})


def _payload(response) -> dict:
    return json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_root_and_transaction_views_require_admin(tmp_path):
    state = _state(tmp_path)
    root = adapter.RootView(state.hass, state)
    transaction = adapter.TransactionView(state.hass, state)
    request = _Request(admin=False)

    assert root.requires_auth is True
    assert transaction.requires_auth is True

    root_response = await root.post(request, "bootstrap")
    get_response = await transaction.get(request, "tx-missing", "readback")
    post_response = await transaction.post(request, "tx-missing", "activate")

    for response in (root_response, get_response, post_response):
        assert response.status == 403
        assert _payload(response) == {"error_code": "admin_required"}


@pytest.mark.asyncio
async def test_bootstrap_rejects_existing_dashboard_metadata_collision(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    ensure_preview = AsyncMock(side_effect=ValueError("preview_collision"))
    monkeypatch.setattr(adapter, "_ensure_preview", ensure_preview)

    response = await adapter.RootView(state.hass, state).post(
        _Request(), "bootstrap"
    )

    assert response.status == 422
    assert _payload(response) == {"error_code": "preview_collision"}
    ensure_preview.assert_awaited_once_with(state.hass)


@pytest.mark.asyncio
async def test_bootstrap_rejects_legacy_aurora_dashboard_without_creating_a_third_target(
    tmp_path, monkeypatch
):
    legacy = _Dashboard({adapter.CONF_URL_PATH: adapter.LEGACY_PREVIEW})
    monkeypatch.setattr(
        adapter,
        "_dashboards",
        AsyncMock(return_value=(SimpleNamespace(), {adapter.LEGACY_PREVIEW: legacy})),
    )

    with pytest.raises(ValueError, match="legacy_preview_collision"):
        await adapter._ensure_preview(_hass(tmp_path))


@pytest.mark.asyncio
async def test_stage_persists_verified_transaction_and_rejects_nonce_replay(
    tmp_path, monkeypatch
):
    state = _state(tmp_path)
    manifest_sha = "1" * 64
    package_sha = "2" * 64
    dashboard_sha = "3" * 64
    package = b"signed-package"
    dashboard = b"export const aurora = true;"
    manifest = {"nonce": "nonce-unique-001", "expires_at": "2099-01-01T00:00:00Z"}
    validate = Mock(return_value=(manifest_sha, package_sha, dashboard_sha))
    monkeypatch.setattr(adapter, "_validate_manifest", validate)
    body = {
        "dashboard_target": adapter.PREVIEW,
        "preview_only": True,
        "manifest": manifest,
        "artifacts": {
            "package": base64.b64encode(package).decode(),
            "dashboard": base64.b64encode(dashboard).decode(),
        },
    }

    view = adapter.RootView(state.hass, state)
    staged = await view.post(_Request(body), "stage")

    assert staged.status == 200
    staged_body = _payload(staged)
    expected_revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    assert staged_body["status"] == "verified"
    assert staged_body["revision"] == expected_revision
    assert staged_body["staged_revision"] == expected_revision
    transaction_id = staged_body["transaction_id"]
    transaction = state.tx(transaction_id)
    assert transaction is not None
    assert state.journal["nonces"][manifest["nonce"]] == transaction_id
    revision_dir = tmp_path / "staged" / expected_revision
    assert (revision_dir / "aurora-preview-package.tar.gz").read_bytes() == package
    assert (revision_dir / "aurora-preview-dashboard.js").read_bytes() == dashboard
    validate.assert_called_once_with(state.hass, manifest, package, dashboard)

    replay = await view.post(_Request(body), "stage")

    assert replay.status == 409
    assert _payload(replay) == {"error_code": "manifest_replay"}
    assert len(state.journal["transactions"]) == 1


@pytest.mark.asyncio
async def test_transaction_readback_exposes_verification_hashes_only(tmp_path):
    import hashlib

    manifest = b'{"schema_version":1}'
    package = b"package-readback"
    dashboard = b"dashboard-readback"
    manifest_sha = hashlib.sha256(manifest).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256((manifest_sha + package_sha + dashboard_sha).encode()).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(manifest)
    (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard)
    transaction = {
        "transaction_id": "tx-readback",
        "revision": revision,
        "status": "verified",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "target": adapter.PREVIEW,
        "revision_dir": str(revision_dir),
    }
    state = _state(tmp_path, {"transactions": {"tx-readback": transaction}})

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), "tx-readback", "readback"
    )

    assert response.status == 200
    assert _payload(response) == {
        key: transaction[key]
        for key in (
            "transaction_id",
            "revision",
            "status",
            "manifest_sha256",
            "package_sha256",
            "dashboard_sha256",
            "target",
        )
    } | {"verified": True}


@pytest.mark.asyncio
async def test_activate_revalidates_and_installs_exact_staged_revision(
    tmp_path, monkeypatch
):
    manifest = {"schema_version": 1}
    manifest_raw = adapter._json_bytes(manifest)
    package = b"staged-package-bytes"
    dashboard = b"staged-dashboard-bytes"
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(manifest_raw)
    (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard)
    transaction = {
        "transaction_id": "tx-active",
        "revision": revision,
        "status": "verified",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "revision_dir": str(revision_dir),
    }
    state = _state(tmp_path, {"transactions": {"tx-active": transaction}})
    validate_package = Mock()
    component_members = {
        filename: f"component-{filename}".encode()
        for filename in adapter.APPROVED_COMPONENT_FILES
    }
    validate_package = Mock(return_value=component_members)
    activate_component = Mock(return_value="component-binding")
    ensure_preview = AsyncMock(return_value=(False, adapter.PREVIEW))
    save_preview_asset = AsyncMock(
        return_value="aurora-preview-dashboard-dashboardhash.js"
    )
    load_preview = AsyncMock(return_value=(_Dashboard({"views": []}), {"views": []}))
    monkeypatch.setattr(adapter, "_validate_package", validate_package)
    monkeypatch.setattr(adapter, "_activate_component_package", activate_component)
    monkeypatch.setattr(adapter, "_ensure_preview", ensure_preview)
    monkeypatch.setattr(adapter, "_save_preview_asset", save_preview_asset)
    monkeypatch.setattr(adapter, "_load_dashboard", load_preview)

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), "tx-active", "activate"
    )

    assert response.status == 200
    assert _payload(response) == {
        "activated": True,
        "active_revision": revision,
        "status": "activated",
    }
    validate_package.assert_called_once_with(package)
    activate_component.assert_called_once_with(
        state.hass, state, transaction, component_members
    )
    ensure_preview.assert_awaited_once_with(state.hass)
    save_preview_asset.assert_awaited_once_with(state.hass, dashboard)
    assert state.journal["active_preview"] == revision
    assert transaction["status"] == "activated"
    assert "activated_at" in transaction


def test_component_activation_is_fixed_scope_and_rollback_restores_prestate(tmp_path):
    state = _state(tmp_path)
    transaction = {"transaction_id": "tx-component-fixed-scope"}
    destination = tmp_path / "custom_components" / adapter.COMPONENT_DOMAIN
    destination.mkdir(parents=True)
    (destination / "legacy.py").write_bytes(b"legacy")
    members = {
        filename: f"candidate-{filename}".encode()
        for filename in adapter.APPROVED_COMPONENT_FILES
    }

    binding = adapter._activate_component_package(
        state.hass, state, transaction, members
    )

    assert binding == adapter._component_binding(members)
    assert {path.name for path in destination.iterdir()} == set(
        adapter.APPROVED_COMPONENT_FILES
    )
    assert all(
        (destination / filename).read_bytes() == data
        for filename, data in members.items()
    )

    adapter._restore_component_prestate(state.hass, state, transaction)

    assert {path.name for path in destination.iterdir()} == {"legacy.py"}
    assert (destination / "legacy.py").read_bytes() == b"legacy"
    assert transaction["component_activation_attempt"] == 1

    adapter._activate_component_package(state.hass, state, transaction, members)
    assert transaction["component_activation_attempt"] == 2
    adapter._restore_component_prestate(state.hass, state, transaction)
    assert (destination / "legacy.py").read_bytes() == b"legacy"


@pytest.mark.asyncio
async def test_dashboard_activation_failure_restores_component_and_is_retryable(
    tmp_path, monkeypatch
):
    manifest = {"schema_version": 1}
    manifest_raw = adapter._json_bytes(manifest)
    package = b"retry-package"
    dashboard = b"retry-dashboard"
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(manifest_raw)
    (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard)
    transaction = {
        "transaction_id": "tx-activation-retry",
        "revision": revision,
        "status": "verified",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "revision_dir": str(revision_dir),
    }
    state = _state(
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )
    destination = tmp_path / "custom_components" / adapter.COMPONENT_DOMAIN
    destination.mkdir(parents=True)
    (destination / "legacy.py").write_bytes(b"legacy")
    members = {
        filename: f"retry-{filename}".encode()
        for filename in adapter.APPROVED_COMPONENT_FILES
    }
    preview = _Dashboard({"resources": []})
    monkeypatch.setattr(adapter, "_validate_package", Mock(return_value=members))
    monkeypatch.setattr(adapter, "_ensure_preview", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(return_value=(preview, {"resources": []})),
    )
    save_asset = AsyncMock(
        side_effect=[
            RuntimeError("dashboard-save-failed"),
            "aurora-preview-dashboard-retry.js",
        ]
    )
    monkeypatch.setattr(adapter, "_save_preview_asset", save_asset)
    view = adapter.TransactionView(state.hass, state)

    failed = await view.post(
        _Request(), transaction["transaction_id"], "activate"
    )

    assert failed.status == 500
    assert transaction["status"] == "verified"
    assert (destination / "legacy.py").read_bytes() == b"legacy"
    assert transaction["component_activation_attempt"] == 1

    retried = await view.post(
        _Request(), transaction["transaction_id"], "activate"
    )

    assert retried.status == 200
    assert transaction["status"] == "activated"
    assert transaction["component_activation_attempt"] == 2
    assert all(
        (destination / filename).read_bytes() == data
        for filename, data in members.items()
    )


@pytest.mark.asyncio
async def test_active_readback_binds_component_bytes_and_dashboard_resource(
    tmp_path, monkeypatch
):
    manifest = {"schema_version": 1}
    manifest_raw = adapter._json_bytes(manifest)
    package = b"bound-package"
    dashboard_bytes = b"bound-dashboard"
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard_bytes).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(manifest_raw)
    (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard_bytes)

    members = {
        filename: f"bound-{filename}".encode()
        for filename in adapter.APPROVED_COMPONENT_FILES
    }
    component_root = tmp_path / "custom_components" / adapter.COMPONENT_DOMAIN
    component_root.mkdir(parents=True)
    for filename, data in members.items():
        (component_root / filename).write_bytes(data)
    asset_name = f"aurora-preview-dashboard-{dashboard_sha}.js"
    asset_path = tmp_path / "www" / "aurora" / "revisions" / asset_name
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(dashboard_bytes)
    dashboard_config = {
        "resources": [
            {
                "url": adapter.DASHBOARD_URL_PREFIX + asset_name,
                "res_type": "module",
            }
        ]
    }
    transaction = {
        "transaction_id": "tx-active-readback",
        "revision": revision,
        "status": "activated",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "target": adapter.PREVIEW,
        "revision_dir": str(revision_dir),
        "component_binding_sha256": adapter._component_binding(members),
        "active_dashboard_asset": asset_name,
    }
    state = _state(
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )
    monkeypatch.setattr(adapter, "_validate_package", Mock(return_value=members))
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(return_value=(_Dashboard(dashboard_config), dashboard_config)),
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 200
    payload = _payload(response)
    assert payload["active_package_sha256"] == adapter._component_binding(members)
    assert payload["active_package_verified"] is True
    assert payload["active_dashboard_verified"] is True

    (component_root / "coordinator.py").write_bytes(b"tampered")
    tampered = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )
    assert tampered.status == 409
    assert _payload(tampered) == {"error_code": "active_integrity_failed"}


@pytest.mark.asyncio
async def test_promotion_inspection_is_non_mutating_and_promotion_requires_receipt(
    tmp_path, monkeypatch
):
    revision = "revision-promote"
    transaction = {
        "transaction_id": "tx-promote",
        "revision": revision,
        "status": "activated",
        "manifest_sha256": "a" * 64,
        "package_sha256": "b" * 64,
        "dashboard_sha256": "c" * 64,
    }
    state = _state(
        tmp_path,
        {
            "transactions": {"tx-promote": transaction},
            "active_preview": revision,
            "production_revision": "revision-before",
        },
    )
    preview_config = {"views": [{"title": "Verified preview"}]}
    production_config = {"views": [{"title": "Current production"}]}
    preview = _Dashboard(preview_config)
    production = _Dashboard(production_config)

    async def load_dashboard(_hass, url_path):
        if url_path == adapter.PREVIEW:
            return preview, preview_config
        assert url_path == adapter.PRODUCTION
        return production, production_config

    verify_signature = Mock()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)
    monkeypatch.setattr(adapter, "_verify_signature", verify_signature)
    monkeypatch.setattr(adapter, "_verify_active_transaction", AsyncMock())
    monkeypatch.setattr(adapter, "_now", lambda: now)
    view = adapter.RootView(state.hass, state)

    inspection = await view.post(
        _Request({"preview_revision": revision, "inspect": True}),
        "promote-home-command",
    )

    assert inspection.status == 200
    assert _payload(inspection) == {
        "preview_revision": revision,
        "status": "activated",
        "active_revision": revision,
        "target_dashboard": adapter.PRODUCTION,
        "preview_config_sha256": adapter._config_sha256(preview_config),
        "production_revision": "revision-before",
        "expected_production_config_sha256": adapter._config_sha256(production_config),
        "verified": True,
    }
    production.async_save.assert_not_awaited()
    assert state.journal["production_revision"] == "revision-before"

    missing_receipt = await view.post(
        _Request({"preview_revision": revision}), "promote-home-command"
    )
    assert missing_receipt.status == 422
    assert _payload(missing_receipt) == {
        "error_code": "validation_receipt_required"
    }

    receipt = {
        "schema_version": 1,
        "preview_revision": revision,
        "dashboard_target": adapter.PREVIEW,
        "physical_validation": True,
        "issued_at": now.isoformat(),
        "nonce": "validation-nonce-route-001",
        "signer": "validation-route",
        "signature": "fixture-signature",
        "expected_production_revision": "revision-before",
        "preview_config_sha256": adapter._config_sha256(preview_config),
        "expected_production_config_sha256": adapter._config_sha256(
            production_config
        ),
        "device_results": [
            {"device_id": device, "passed": True}
            for device in ("mobile", "kiosk", "tablet", "laptop", "desktop")
        ],
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "manifest_sha256": transaction["manifest_sha256"],
        "package_sha256": transaction["package_sha256"],
        "dashboard_sha256": transaction["dashboard_sha256"],
    }
    promoted = await view.post(
        _Request(
            {
                "preview_revision": revision,
                "expected_production_revision": "revision-before",
                "receipt": receipt,
            }
        ),
        "promote-home-command",
    )

    assert promoted.status == 200
    assert _payload(promoted) == {
        "promoted": True,
        "active_revision": revision,
        "previous_revision": "revision-before",
        "preview_config_sha256": adapter._config_sha256(preview_config),
        "expected_production_config_sha256": adapter._config_sha256(production_config),
    }
    verify_signature.assert_called_once_with(
        state.hass, receipt, prefix="validation-"
    )
    production.async_save.assert_awaited_once_with(preview_config)
    assert state.journal["previous_production"] == {
        "revision": "revision-before",
        "config": production_config,
        "config_sha256": adapter._config_sha256(production_config),
    }
    assert state.journal["production_revision"] == revision
    assert state.journal["production_config_sha256"] == adapter._config_sha256(
        preview_config
    )
    assert transaction["status"] == "promoted"


@pytest.mark.asyncio
async def test_promotion_inspection_fails_when_active_bytes_are_not_verified(
    tmp_path, monkeypatch
):
    revision = "revision-inspection-tampered"
    transaction = {
        "transaction_id": "tx-inspection-tampered",
        "revision": revision,
        "status": "activated",
    }
    state = _state(
        tmp_path,
        {
            "transactions": {transaction["transaction_id"]: transaction},
            "active_preview": revision,
        },
    )
    monkeypatch.setattr(
        adapter,
        "_verify_active_transaction",
        AsyncMock(side_effect=ValueError("tampered")),
    )

    response = await adapter.RootView(state.hass, state).post(
        _Request({"preview_revision": revision, "inspect": True}),
        "promote-home-command",
    )

    assert response.status == 409
    assert _payload(response) == {"error_code": "preview_integrity_failed"}


@pytest.mark.asyncio
async def test_rollback_restores_and_rotates_immediate_prior_production(
    tmp_path, monkeypatch
):
    prior_config = {"views": [{"title": "Prior production"}]}
    current_config = {"views": [{"title": "Current production"}]}
    production = _Dashboard(current_config)
    state = _state(
        tmp_path,
        {
            "production_revision": "revision-current",
            "production_config_sha256": adapter._config_sha256(current_config),
            "previous_production": {
                "revision": "revision-prior",
                "config": prior_config,
                "config_sha256": adapter._config_sha256(prior_config),
            },
        },
    )
    load_dashboard = AsyncMock(return_value=(production, current_config))
    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)

    response = await adapter.RootView(state.hass, state).post(
        _Request(), "rollback-home-command"
    )

    assert response.status == 200
    assert _payload(response) == {
        "rolled_back": True,
        "active_revision": "revision-prior",
    }
    load_dashboard.assert_awaited_once_with(state.hass, adapter.PRODUCTION)
    production.async_save.assert_awaited_once_with(prior_config)
    assert state.journal["production_revision"] == "revision-prior"
    assert state.journal["production_config_sha256"] == adapter._config_sha256(
        prior_config
    )
    assert state.journal["previous_production"] is None

    repeated = await adapter.RootView(state.hass, state).post(
        _Request(), "rollback-home-command"
    )
    assert repeated.status == 409
    assert _payload(repeated) == {"error_code": "no_prior_production_revision"}


def test_corrupt_journal_fails_closed(tmp_path):
    root = tmp_path / ".storage" / "aurora_deploy_preview"
    root.mkdir(parents=True)
    (root / "journal.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="aurora_deploy_journal_corrupt"):
        adapter.AuroraState._create_sync(_hass(tmp_path))


@pytest.mark.asyncio
async def test_request_rate_limit_fails_closed(tmp_path):
    state = _state(tmp_path)
    state.request_times.extend([adapter.time.monotonic()] * adapter.MAX_REQUESTS_PER_MINUTE)

    response = await adapter.RootView(state.hass, state).post(_Request(), "bootstrap")

    assert response.status == 429
    assert _payload(response) == {"error_code": "rate_limited"}


@pytest.mark.asyncio
async def test_transaction_rollback_restores_preview_snapshot(tmp_path, monkeypatch):
    transaction = {
        "transaction_id": "tx-preview-rollback",
        "revision": "revision-preview",
        "status": "activated",
        "preview_before": {"views": [{"title": "Before"}]},
        "preview_revision_before": "revision-before-preview",
    }
    state = _state(
        tmp_path,
        {
            "transactions": {transaction["transaction_id"]: transaction},
            "active_preview": transaction["revision"],
        },
    )
    preview = _Dashboard({"views": [{"title": "After"}]})
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(return_value=(preview, {"views": [{"title": "After"}]})),
    )

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "rollback"
    )

    assert response.status == 200
    assert _payload(response) == {
        "rolled_back": True,
        "active_revision": "revision-before-preview",
        "preview_active": True,
        "status": "rolled_back",
    }
    preview.async_save.assert_awaited_once_with(transaction["preview_before"])
    assert state.journal["active_preview"] == "revision-before-preview"
    assert transaction["status"] == "rolled_back"


@pytest.mark.asyncio
async def test_reload_without_registered_backend_fails_closed(tmp_path):
    transaction = {
        "transaction_id": "tx-reload",
        "revision": "revision-reload",
        "status": "activated",
    }
    state = _state(tmp_path, {"transactions": {transaction["transaction_id"]: transaction}})

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "reload"
    )

    assert response.status == 409
    assert _payload(response) == {"error_code": "restart_required"}
