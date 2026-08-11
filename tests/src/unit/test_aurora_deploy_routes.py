"""Route-level tests for the fixed-scope Aurora deployment adapter."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
        self.async_load = AsyncMock(side_effect=lambda *_args: self.config)

        async def save(updated: dict) -> None:
            self.config = json.loads(json.dumps(updated))

        self.async_save = AsyncMock(side_effect=save)


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


def _validation_signer(state, signer: str = "validation-e2e") -> Ed25519PrivateKey:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trusted_path = Path(state.hass.config.path("aurora_deploy_trusted_keys.json"))
    trusted_path.write_text(
        json.dumps({signer: base64.b64encode(public).decode()}), encoding="utf-8"
    )
    return private


def _sign_receipt(receipt: dict, private: Ed25519PrivateKey) -> dict:
    signed = json.loads(json.dumps(receipt))
    signed["signature"] = base64.b64encode(
        private.sign(adapter._canonical_manifest(signed))
    ).decode()
    return signed


def _v2_profiles() -> list[dict]:
    return [
        {
            "profile_id": profile_id,
            "width": width,
            "height": height,
            "passed": True,
            "screenshot_sha256": hashlib.sha256(profile_id.encode()).hexdigest(),
        }
        for profile_id, width, height in adapter.AUTOMATED_E2E_PROFILES
    ]


def _v2_receipt(
    inspection: dict,
    now: datetime,
    private: Ed25519PrivateKey,
    *,
    nonce: str,
) -> dict:
    return _sign_receipt(
        {
            "schema_version": 2,
            "preview_revision": inspection["preview_revision"],
            "transaction_id": inspection["transaction_id"],
            "operation_id": "operation-" + nonce,
            "expected_production_revision": inspection["production_revision"],
            "preview_config_sha256": inspection["preview_config_sha256"],
            "expected_production_config_sha256": inspection[
                "expected_production_config_sha256"
            ],
            "dashboard_target": adapter.PREVIEW,
            "validation_kind": "automated_e2e",
            "e2e_evidence_sha256": hashlib.sha256(
                b"canonical-e2e-evidence"
            ).hexdigest(),
            "profile_results": _v2_profiles(),
            "manifest_sha256": inspection["manifest_sha256"],
            "package_sha256": inspection["package_sha256"],
            "dashboard_sha256": inspection["dashboard_sha256"],
            "audience": inspection["audience"],
            "action": "promote_home_command",
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "nonce": nonce,
            "signer": "validation-e2e",
        },
        private,
    )


def _promotion_context(tmp_path, monkeypatch, suffix: str):
    revision = f"revision-{suffix}"
    dashboard_sha = hashlib.sha256(f"dashboard-{suffix}".encode()).hexdigest()
    transaction = {
        "transaction_id": f"tx-{suffix}",
        "revision": revision,
        "status": "activated",
        "manifest_sha256": hashlib.sha256(f"manifest-{suffix}".encode()).hexdigest(),
        "package_sha256": hashlib.sha256(f"package-{suffix}".encode()).hexdigest(),
        "dashboard_sha256": dashboard_sha,
        "active_dashboard_asset": f"aurora-preview-dashboard-{dashboard_sha}.js",
    }
    state = _state(
        tmp_path,
        {
            "transactions": {transaction["transaction_id"]: transaction},
            "active_preview": revision,
            "production_revision": f"production-{suffix}",
        },
    )
    asset = (
        tmp_path
        / "www"
        / "aurora"
        / "revisions"
        / transaction["active_dashboard_asset"]
    )
    asset.parent.mkdir(parents=True)
    asset.write_bytes(f"dashboard-{suffix}".encode())
    preview = _Dashboard(
        {
            "views": [{"title": f"Preview {suffix}"}],
            "resources": [
                {
                    "url": adapter.DASHBOARD_URL_PREFIX
                    + transaction["active_dashboard_asset"],
                    "res_type": "module",
                }
            ],
        }
    )
    production = _Dashboard({"views": [{"title": f"Production {suffix}"}]})

    async def load_dashboard(_hass, url_path):
        selected = preview if url_path == adapter.PREVIEW else production
        return selected, selected.config

    now = datetime(2031, 2, 3, tzinfo=UTC)
    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)
    monkeypatch.setattr(adapter, "_verify_active_transaction", AsyncMock())
    monkeypatch.setattr(adapter, "_now", lambda: now)
    return state, transaction, preview, production, now


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

    response = await adapter.RootView(state.hass, state).post(_Request(), "bootstrap")

    assert response.status == 422
    assert _payload(response) == {"error_code": "preview_collision"}
    ensure_preview.assert_awaited_once_with(state.hass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "icon"),
    [
        ("Aurora Preview", "mdi:aurora"),
        ("Aurora V9 Preview", "mdi:home-analytics"),
    ],
)
async def test_bootstrap_accepts_closed_preview_metadata_compatibility_without_mutation(
    tmp_path, monkeypatch, title, icon
):
    state = _state(tmp_path)
    metadata = {
        adapter.CONF_URL_PATH: adapter.PREVIEW,
        adapter.CONF_TITLE: title,
        adapter.CONF_ICON: icon,
        adapter.CONF_SHOW_IN_SIDEBAR: False,
        adapter.CONF_REQUIRE_ADMIN: True,
    }
    existing = _Dashboard(metadata)
    dashboards = {adapter.PREVIEW: existing}
    monkeypatch.setattr(
        adapter,
        "_dashboards",
        AsyncMock(return_value=(SimpleNamespace(), dashboards)),
    )

    response = await adapter.RootView(state.hass, state).post(_Request(), "bootstrap")

    assert response.status == 200
    assert _payload(response) == {
        "dashboard_target": adapter.PREVIEW,
        "created": False,
        "production_unchanged": True,
    }
    assert existing.config == metadata
    existing.async_save.assert_not_awaited()
    assert dashboards == {adapter.PREVIEW: existing}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "icon", "url_path", "show_in_sidebar", "require_admin"),
    [
        ("Aurora Preview (drift)", "mdi:aurora", adapter.PREVIEW, False, True),
        ("Aurora Preview", "mdi:weather-sunny", adapter.PREVIEW, False, True),
        ("Aurora V9 Preview", "mdi:aurora", adapter.PREVIEW, False, True),
        ("Aurora Preview", "mdi:aurora", adapter.PREVIEW, True, True),
        ("Aurora V9 Preview", "mdi:home-analytics", adapter.PREVIEW, False, False),
        (
            "Aurora V9 Preview",
            "mdi:home-analytics",
            "aurora-preview-copy",
            False,
            True,
        ),
    ],
)
async def test_bootstrap_rejects_preview_metadata_outside_closed_compatibility(
    tmp_path,
    monkeypatch,
    title,
    icon,
    url_path,
    show_in_sidebar,
    require_admin,
):
    state = _state(tmp_path)
    metadata = {
        adapter.CONF_URL_PATH: url_path,
        adapter.CONF_TITLE: title,
        adapter.CONF_ICON: icon,
        adapter.CONF_SHOW_IN_SIDEBAR: show_in_sidebar,
        adapter.CONF_REQUIRE_ADMIN: require_admin,
    }
    existing = _Dashboard(metadata)
    dashboards = {adapter.PREVIEW: existing}
    monkeypatch.setattr(
        adapter,
        "_dashboards",
        AsyncMock(return_value=(SimpleNamespace(), dashboards)),
    )

    response = await adapter.RootView(state.hass, state).post(_Request(), "bootstrap")

    assert response.status == 422
    assert _payload(response) == {"error_code": "preview_collision"}
    assert existing.config == metadata
    existing.async_save.assert_not_awaited()
    assert dashboards == {adapter.PREVIEW: existing}


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
    state = _state(tmp_path, {"active_preview": "revision-before-stage"})
    package = b"signed-package"
    dashboard = b"export const aurora = true;"
    manifest = {"nonce": "nonce-unique-001", "expires_at": "2099-01-01T00:00:00Z"}
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    validate = Mock(return_value=(manifest_sha, package_sha, dashboard_sha))
    monkeypatch.setattr(adapter, "_validate_manifest", validate)
    body = {
        "transaction_id": "transaction-stage-route-001",
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
    assert staged_body["previous_revision"] == "revision-before-stage"
    transaction_id = staged_body["transaction_id"]
    transaction = state.tx(transaction_id)
    assert transaction is not None
    assert transaction["stage_transition"]["status"] == "committed"
    assert state.journal["nonces"][manifest["nonce"]] == transaction_id
    revision_dir = tmp_path / "staged" / expected_revision
    assert (revision_dir / "aurora-preview-package.tar.gz").read_bytes() == package
    assert (revision_dir / "aurora-preview-dashboard.js").read_bytes() == dashboard
    validate.assert_called_once_with(state.hass, manifest, package, dashboard)

    replay = await view.post(_Request(body), "stage")

    assert replay.status == 200
    assert _payload(replay)["idempotent"] is True
    assert len(state.journal["transactions"]) == 1

    collision_body = json.loads(json.dumps(body))
    collision_body["artifacts"]["dashboard"] = base64.b64encode(
        b"different-dashboard"
    ).decode()
    collision = await view.post(_Request(collision_body), "stage")
    assert collision.status == 409
    assert _payload(collision) == {"error_code": "transaction_id_conflict"}

    nonce_replay_body = json.loads(json.dumps(body))
    nonce_replay_body["transaction_id"] = "transaction-stage-route-002"
    nonce_replay = await view.post(_Request(nonce_replay_body), "stage")
    assert nonce_replay.status == 409
    assert _payload(nonce_replay) == {"error_code": "manifest_replay"}


@pytest.mark.asyncio
@pytest.mark.parametrize("staged_state", ["complete", "partial", "absent"])
async def test_transaction_readback_reconciles_lost_stage_outcome(
    tmp_path, staged_state
):
    manifest = {"schema_version": 1}
    package = b"lost-stage-package"
    dashboard = b"lost-stage-dashboard"
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    transaction_id = f"transaction-lost-stage-{staged_state}"
    request_sha = "d" * 64
    transaction = {
        "transaction_id": transaction_id,
        "revision": revision,
        "status": "staging",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "target": adapter.PREVIEW,
        "stage_request_sha256": request_sha,
        "stage_previous_revision": "revision-before-stage",
        "stage_transition": {
            "status": "prepared",
            "transaction_id": transaction_id,
            "revision": revision,
            "request_sha256": request_sha,
            "manifest_sha256": manifest_sha,
            "package_sha256": package_sha,
            "dashboard_sha256": dashboard_sha,
        },
    }
    revision_dir = tmp_path / "staged" / revision
    if staged_state != "absent":
        revision_dir.mkdir(parents=True)
        (revision_dir / "manifest.json").write_bytes(adapter._json_bytes(manifest))
        (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    if staged_state == "complete":
        (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard)
    state = _state(
        tmp_path,
        {"transactions": {transaction_id: transaction}},
    )
    original_save = state.save
    state.save = Mock(wraps=original_save)
    view = adapter.TransactionView(state.hass, state)

    response = await view.get(_Request(), transaction_id, "readback")

    assert response.status == 200
    payload = _payload(response)
    staged_bytes_complete = staged_state == "complete"
    expected_status = "verified" if staged_bytes_complete else "aborted"
    assert payload["status"] == expected_status
    assert payload["stage_status"] == (
        "committed" if staged_bytes_complete else "aborted"
    )
    assert payload["verified"] is staged_bytes_complete
    assert payload["previous_revision"] == "revision-before-stage"
    assert state.save.call_count == 1

    repeated = await view.get(_Request(), transaction_id, "readback")
    assert repeated.status == 200
    assert _payload(repeated)["status"] == expected_status
    assert state.save.call_count == 1


@pytest.mark.asyncio
async def test_transaction_readback_exposes_verification_hashes_only(tmp_path):
    import hashlib

    manifest = b'{"schema_version":1}'
    package = b"package-readback"
    dashboard = b"dashboard-readback"
    manifest_sha = hashlib.sha256(manifest).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
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
    } | {
        "verified": True,
        "staged_package_verified": True,
        "previous_revision": None,
        "dashboard_resource_present": False,
    }


@pytest.mark.asyncio
async def test_activate_revalidates_dashboard_without_mutating_backend(
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
    validate_package = Mock(return_value={})
    ensure_preview = AsyncMock(return_value=(False, adapter.PREVIEW))
    preview = _Dashboard({"views": []})
    load_preview = AsyncMock(side_effect=lambda *_args: (preview, preview.config))
    save_preview_asset = AsyncMock(wraps=adapter._save_preview_asset)
    monkeypatch.setattr(adapter, "_validate_package", validate_package)
    monkeypatch.setattr(adapter, "_ensure_preview", ensure_preview)
    monkeypatch.setattr(adapter, "_save_preview_asset", save_preview_asset)
    monkeypatch.setattr(adapter, "_load_dashboard", load_preview)
    verify_active = AsyncMock(return_value=dashboard_sha)
    monkeypatch.setattr(adapter, "_verify_active_bindings", verify_active)
    backend = tmp_path / "custom_components" / adapter.COMPONENT_DOMAIN
    backend.mkdir(parents=True)
    sentinel = backend / "sentinel.py"
    sentinel.write_bytes(b"backend-lifecycle-owned-elsewhere")

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), "tx-active", "activate"
    )

    assert response.status == 200
    assert _payload(response) == {
        "activated": True,
        "active_revision": revision,
        "previous_revision": None,
        "status": "activated",
        "restart_required": False,
        "backend_unchanged": True,
    }
    validate_package.assert_called_once_with(package)
    ensure_preview.assert_awaited_once_with(state.hass)
    save_preview_asset.assert_awaited_once_with(state.hass, dashboard)
    assert sentinel.read_bytes() == b"backend-lifecycle-owned-elsewhere"
    assert state.journal["active_preview"] == revision
    assert transaction["status"] == "activated"
    assert "activated_at" in transaction

    repeated = await adapter.TransactionView(state.hass, state).post(
        _Request(), "tx-active", "activate"
    )
    assert repeated.status == 200
    assert _payload(repeated)["idempotent"] is True
    assert save_preview_asset.await_count == 1


@pytest.mark.asyncio
async def test_activation_rejects_untracked_aurora_resource_prestate(
    tmp_path, monkeypatch
):
    manifest = {"schema_version": 1}
    package = b"prestate-package"
    dashboard = b"prestate-dashboard"
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(adapter._json_bytes(manifest))
    (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard)
    transaction = {
        "transaction_id": "tx-untracked-prestate",
        "revision": revision,
        "status": "verified",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "target": adapter.PREVIEW,
    }
    state = _state(
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )
    preview = _Dashboard(
        {
            "resources": [
                {"url": adapter.DASHBOARD_URL, "res_type": "module"}
            ]
        }
    )
    monkeypatch.setattr(adapter, "_validate_package", Mock(return_value={}))
    monkeypatch.setattr(
        adapter, "_ensure_preview", AsyncMock(return_value=(False, adapter.PREVIEW))
    )
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (preview, preview.config)),
    )
    save_preview_asset = AsyncMock()
    monkeypatch.setattr(adapter, "_save_preview_asset", save_preview_asset)

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "activate"
    )

    assert response.status == 409
    assert _payload(response) == {"error_code": "preview_prestate_invalid"}
    save_preview_asset.assert_not_awaited()
    assert transaction["status"] == "verified"


@pytest.mark.asyncio
async def test_dashboard_activation_failure_is_retryable_and_backend_unchanged(
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
    preview = _Dashboard({"resources": []})
    monkeypatch.setattr(adapter, "_validate_package", Mock(return_value={}))
    monkeypatch.setattr(adapter, "_ensure_preview", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (preview, preview.config)),
    )
    save_attempts = 0

    async def save_asset_effect(_hass, dashboard_bytes):
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise RuntimeError("dashboard-save-failed")
        asset_name = (
            f"aurora-preview-dashboard-{hashlib.sha256(dashboard_bytes).hexdigest()}.js"
        )
        await preview.async_save(
            adapter._dashboard_config_with_asset(preview.config, asset_name)
        )
        return asset_name

    save_asset = AsyncMock(side_effect=save_asset_effect)
    monkeypatch.setattr(adapter, "_save_preview_asset", save_asset)
    monkeypatch.setattr(
        adapter, "_verify_active_bindings", AsyncMock(return_value=dashboard_sha)
    )
    view = adapter.TransactionView(state.hass, state)

    failed = await view.post(_Request(), transaction["transaction_id"], "activate")

    assert failed.status == 500
    assert transaction["status"] == "verified"
    assert (destination / "legacy.py").read_bytes() == b"legacy"

    retried = await view.post(_Request(), transaction["transaction_id"], "activate")

    assert retried.status == 200
    assert transaction["status"] == "activated"
    assert (destination / "legacy.py").read_bytes() == b"legacy"


@pytest.mark.asyncio
async def test_active_readback_binds_dashboard_resource_only(tmp_path, monkeypatch):
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
        "active_dashboard_asset": asset_name,
        "active_preview_config_sha256": adapter._config_sha256(dashboard_config),
    }
    state = _state(
        tmp_path,
        {
            "transactions": {transaction["transaction_id"]: transaction},
            "active_preview": revision,
        },
    )
    monkeypatch.setattr(adapter, "_validate_package", Mock(return_value={}))
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
    assert payload["staged_package_verified"] is True
    assert payload["active_dashboard_sha256"] == dashboard_sha
    assert payload["active_dashboard_verified"] is True
    assert payload["dashboard_resource_present"] is True
    assert payload["active_dashboard_resource_url"] == (
        adapter.DASHBOARD_URL_PREFIX + asset_name
    )
    assert payload["active_dashboard_size"] == len(dashboard_bytes)

    asset_path.write_bytes(b"tampered")
    tampered = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )
    assert tampered.status == 409
    assert _payload(tampered) == {"error_code": "active_integrity_failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transition_kind", "applied"),
    [
        ("activate", True),
        ("activate", False),
        ("rollback", True),
        ("rollback", False),
    ],
)
async def test_transaction_readback_reconciles_lost_preview_outcome_without_resave(
    tmp_path, monkeypatch, transition_kind, applied
):
    manifest = {"schema_version": 1}
    package = b"transaction-recovery-package"
    dashboard = b"transaction-recovery-dashboard"
    manifest_sha = hashlib.sha256(adapter._canonical_manifest(manifest)).hexdigest()
    package_sha = hashlib.sha256(package).hexdigest()
    dashboard_sha = hashlib.sha256(dashboard).hexdigest()
    revision = hashlib.sha256(
        (manifest_sha + package_sha + dashboard_sha).encode()
    ).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(adapter._json_bytes(manifest))
    (revision_dir / "aurora-preview-package.tar.gz").write_bytes(package)
    (revision_dir / "aurora-preview-dashboard.js").write_bytes(dashboard)
    asset_name = f"aurora-preview-dashboard-{dashboard_sha}.js"
    active_asset_path = tmp_path / "www" / "aurora" / "revisions" / asset_name
    active_asset_path.parent.mkdir(parents=True)
    active_asset_path.write_bytes(dashboard)
    prior_dashboard = b"prior-transaction-dashboard"
    prior_dashboard_sha = hashlib.sha256(prior_dashboard).hexdigest()
    prior_asset_name = f"aurora-preview-dashboard-{prior_dashboard_sha}.js"
    prior_asset_path = (
        tmp_path / "www" / "aurora" / "revisions" / prior_asset_name
    )
    prior_asset_path.parent.mkdir(parents=True, exist_ok=True)
    prior_asset_path.write_bytes(prior_dashboard)
    before_config = adapter._dashboard_config_with_asset(
        {"views": [{"title": "Preview before"}]}, prior_asset_name
    )
    active_config = adapter._dashboard_config_with_asset(before_config, asset_name)
    prior_transaction = {
        "transaction_id": "tx-prior-preview",
        "revision": "revision-before-preview",
        "status": "activated",
        "dashboard_sha256": prior_dashboard_sha,
        "active_dashboard_asset": prior_asset_name,
        "active_preview_config_sha256": adapter._config_sha256(before_config),
    }
    transaction = {
        "transaction_id": f"tx-{transition_kind}-{int(applied)}",
        "revision": revision,
        "status": "verified" if transition_kind == "activate" else "activated",
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "dashboard_sha256": dashboard_sha,
        "target": adapter.PREVIEW,
        "active_dashboard_asset": asset_name,
        "preview_before": before_config,
        "preview_config_sha256_before": adapter._config_sha256(before_config),
        "preview_revision_before": "revision-before-preview",
        "active_preview_config_sha256": adapter._config_sha256(active_config),
    }
    if transition_kind == "activate":
        transaction["activation_transition"] = {
            "status": "prepared",
            "action": "activate",
            "transaction_id": transaction["transaction_id"],
            "previous_revision": "revision-before-preview",
            "next_revision": revision,
            "previous_config_sha256": adapter._config_sha256(before_config),
            "next_config_sha256": adapter._config_sha256(active_config),
            "asset_name": asset_name,
        }
        live_config = active_config if applied else before_config
        journal = {
            "transactions": {
                prior_transaction["transaction_id"]: prior_transaction,
                transaction["transaction_id"]: transaction,
            },
            "active_preview": "revision-before-preview",
        }
        transition_key = "activation_transition"
        expected_status = "activated" if applied else "verified"
    else:
        transaction["preview_rollback_transition"] = {
            "status": "prepared",
            "action": "rollback",
            "transaction_id": transaction["transaction_id"],
            "from_status": "activated",
            "from_revision": revision,
            "to_revision": "revision-before-preview",
            "previous_config_sha256": adapter._config_sha256(active_config),
            "next_config_sha256": adapter._config_sha256(before_config),
            "asset_name": asset_name,
        }
        live_config = before_config if applied else active_config
        journal = {
            "transactions": {
                prior_transaction["transaction_id"]: prior_transaction,
                transaction["transaction_id"]: transaction,
            },
            "active_preview": revision,
        }
        transition_key = "preview_rollback_transition"
        expected_status = "rolled_back" if applied else "activated"
    state = _state(tmp_path, journal)
    preview = _Dashboard(live_config)
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (preview, preview.config)),
    )
    monkeypatch.setattr(adapter, "_validate_package", Mock(return_value={}))
    monkeypatch.setattr(
        adapter, "_verify_active_transaction", AsyncMock(return_value=dashboard_sha)
    )
    monkeypatch.setattr(
        adapter, "_verify_active_bindings", AsyncMock(return_value=dashboard_sha)
    )
    original_save = state.save
    state.save = Mock(wraps=original_save)

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 200
    payload = _payload(response)
    assert payload["status"] == expected_status
    assert payload[
        "activation_status" if transition_kind == "activate" else "rollback_status"
    ] == ("committed" if applied else "aborted")
    assert transaction[transition_key]["status"] == (
        "committed" if applied else "aborted"
    )
    if transition_kind == "rollback" and applied:
        assert payload["dashboard_resource_present"] is True
        assert payload["active_dashboard_verified"] is True
        assert payload["active_dashboard_resource_url"] == (
            adapter.DASHBOARD_URL_PREFIX + prior_asset_name
        )
        assert payload["active_dashboard_sha256"] == prior_dashboard_sha
        assert payload["active_dashboard_size"] == len(prior_dashboard)
    assert preview.async_save.await_count == 0
    assert state.save.call_count == 1

    repeated = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )
    assert repeated.status == 200
    assert _payload(repeated)["status"] == expected_status
    assert state.save.call_count == 1


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
    resource_url = (
        adapter.DASHBOARD_URL_PREFIX
        + f"aurora-preview-dashboard-{transaction['dashboard_sha256']}.js"
    )
    preview_config = {
        "views": [{"title": "Verified preview"}],
        "resources": [{"url": resource_url, "res_type": "module"}],
    }
    production_config = {"views": [{"title": "Current production"}]}
    preview = _Dashboard(preview_config)
    production = _Dashboard(production_config)

    async def load_dashboard(_hass, url_path):
        if url_path == adapter.PREVIEW:
            return preview, preview.config
        assert url_path == adapter.PRODUCTION
        return production, production.config

    verify_signature = Mock()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)
    monkeypatch.setattr(adapter, "_verify_signature", verify_signature)
    monkeypatch.setattr(adapter, "_verify_active_transaction", AsyncMock())
    resource_context = {
        "preview_resource_url": resource_url,
        "preview_resource_sha256": transaction["dashboard_sha256"],
        "preview_resource_size": 123,
    }
    monkeypatch.setattr(
        adapter, "_active_resource_context", Mock(return_value=resource_context)
    )
    monkeypatch.setattr(adapter, "_now", lambda: now)
    view = adapter.RootView(state.hass, state)

    inspection = await view.post(
        _Request({"preview_revision": revision, "inspect": True}),
        "promote-home-command",
    )

    assert inspection.status == 200
    assert _payload(inspection) == {
        "preview_revision": revision,
        "transaction_id": transaction["transaction_id"],
        "status": "activated",
        "active_revision": revision,
        "dashboard_target": adapter.PREVIEW,
        "target_dashboard": adapter.PRODUCTION,
        "preview_config_sha256": adapter._config_sha256(preview_config),
        "production_revision": "revision-before",
        "expected_production_config_sha256": adapter._config_sha256(production_config),
        "manifest_sha256": transaction["manifest_sha256"],
        "package_sha256": transaction["package_sha256"],
        "dashboard_sha256": transaction["dashboard_sha256"],
        "audience": state.journal["audience"],
        "verified": True,
        **resource_context,
    }
    production.async_save.assert_not_awaited()
    assert state.journal["production_revision"] == "revision-before"

    missing_receipt = await view.post(
        _Request({"preview_revision": revision}), "promote-home-command"
    )
    assert missing_receipt.status == 422
    assert _payload(missing_receipt) == {"error_code": "validation_receipt_required"}

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
        "expected_production_config_sha256": adapter._config_sha256(production_config),
        "device_results": [
            {"device_id": device, "passed": True}
            for device in ("mobile", "kiosk", "tablet", "laptop", "desktop")
        ],
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "manifest_sha256": transaction["manifest_sha256"],
        "package_sha256": transaction["package_sha256"],
        "dashboard_sha256": transaction["dashboard_sha256"],
    }
    promotion_body = {
        "preview_revision": revision,
        "expected_production_revision": "revision-before",
        "operation_id": "018f3f77-4d52-4cd2-9ce0-b9e9b547b001",
        "receipt": receipt,
    }
    promoted = await view.post(_Request(promotion_body), "promote-home-command")

    assert promoted.status == 200
    promoted_payload = _payload(promoted)
    assert promoted_payload == {
        "promoted": True,
        "operation_id": "018f3f77-4d52-4cd2-9ce0-b9e9b547b001",
        "status": "committed",
        "active_revision": revision,
        "previous_revision": "revision-before",
        "preview_config_sha256": adapter._config_sha256(preview_config),
        "expected_production_config_sha256": adapter._config_sha256(production_config),
        "dashboard_resource_url": resource_url,
        "dashboard_sha256": transaction["dashboard_sha256"],
        "dashboard_size": 123,
        "applied": True,
        "verified": True,
        "dashboard_resource_present": True,
    }
    verify_signature.assert_called_once_with(state.hass, receipt, prefix="validation-")
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
    assert promoted_payload["operation_id"] == "018f3f77-4d52-4cd2-9ce0-b9e9b547b001"

    repeated = await view.post(_Request(promotion_body), "promote-home-command")
    assert repeated.status == 200
    assert _payload(repeated)["idempotent"] is True
    assert _payload(repeated)["verified"] is True
    assert production.async_save.await_count == 1

    collision_receipt = json.loads(json.dumps(receipt))
    collision_receipt["nonce"] = "validation-nonce-route-collision-001"
    collision = await view.post(
        _Request(promotion_body | {"receipt": collision_receipt}),
        "promote-home-command",
    )
    assert collision.status == 409
    assert _payload(collision) == {"error_code": "operation_id_conflict"}

    body_collision = await view.post(
        _Request(promotion_body | {"expected_production_revision": "different-cas"}),
        "promote-home-command",
    )
    assert body_collision.status == 409
    assert _payload(body_collision) == {"error_code": "operation_id_conflict"}

    invalid_id = await view.post(
        _Request(promotion_body | {"operation_id": "not-a-uuid"}),
        "promote-home-command",
    )
    assert invalid_id.status == 422
    assert _payload(invalid_id) == {"error_code": "operation_id_uuid_required"}


@pytest.mark.asyncio
async def test_v2_ed25519_promotion_status_readback_idempotency_and_replay(
    tmp_path, monkeypatch
):
    state, transaction, _preview, production, now = _promotion_context(
        tmp_path, monkeypatch, "v2-happy"
    )
    private = _validation_signer(state)
    view = adapter.RootView(state.hass, state)
    inspected = await view.post(
        _Request({"preview_revision": transaction["revision"], "inspect": True}),
        "promote-home-command",
    )
    inspection = _payload(inspected)
    assert inspected.status == 200
    assert inspection["transaction_id"] == transaction["transaction_id"]
    assert inspection["dashboard_target"] == "aurora-preview"
    assert inspection["target_dashboard"] == "home-command"
    assert inspection["preview_resource_sha256"] == transaction["dashboard_sha256"]
    assert inspection["preview_resource_size"] > 0
    assert inspection["preview_resource_url"].startswith(adapter.DASHBOARD_URL_PREFIX)
    receipt = _v2_receipt(inspection, now, private, nonce="validation-v2-happy-001")

    promoted = await view.post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "operation_id": receipt["operation_id"],
                "expected_production_revision": inspection["production_revision"],
                "receipt": receipt,
            }
        ),
        "promote-home-command",
    )

    promoted_payload = _payload(promoted)
    assert promoted.status == 200
    assert promoted_payload["status"] == "committed"
    operation_id = promoted_payload["operation_id"]
    assert operation_id == receipt["operation_id"]
    assert production.config["views"] == [{"title": "Preview v2-happy"}]

    operation_view = adapter.TransactionView(state.hass, state)
    status = await operation_view.get(_Request(), operation_id, "status")
    assert status.status == 200
    assert _payload(status) == {
        "operation_id": operation_id,
        "action": "promote_home_command",
        "status": "committed",
        "transaction_id": transaction["transaction_id"],
        "preview_revision": transaction["revision"],
        "target_revision": transaction["revision"],
        "expected_production_revision": inspection["production_revision"],
        "expected_production_config_sha256": inspection[
            "expected_production_config_sha256"
        ],
        "preview_config_sha256": inspection["preview_config_sha256"],
        "production_config_sha256": inspection["preview_config_sha256"],
        "created_at": now.isoformat(),
        "completed_at": now.isoformat(),
    }
    readback = await operation_view.get(_Request(), operation_id, "readback")
    assert readback.status == 200
    readback_payload = _payload(readback)
    assert readback_payload["verified"] is True
    assert readback_payload["active_revision"] == transaction["revision"]
    assert (
        readback_payload["live_production_config_sha256"]
        == inspection["preview_config_sha256"]
    )
    assert readback_payload["applied"] is True
    assert (
        readback_payload["dashboard_resource_url"] == inspection["preview_resource_url"]
    )
    assert readback_payload["dashboard_sha256"] == inspection["dashboard_sha256"]
    assert readback_payload["dashboard_size"] == inspection["preview_resource_size"]
    assert readback_payload["dashboard_resource_present"] is True

    original_config = json.loads(json.dumps(production.config))
    operation_record = state.journal["operations"][operation_id]
    original_operation_sha = operation_record["production_config_sha256"]
    wrong_resource_config = json.loads(json.dumps(original_config))
    wrong_resource_config["resources"][0]["url"] = (
        adapter.DASHBOARD_URL_PREFIX + "aurora-preview-dashboard-" + "f" * 64 + ".js"
    )
    production.config = wrong_resource_config
    operation_record["production_config_sha256"] = adapter._config_sha256(
        wrong_resource_config
    )
    wrong_resource = await operation_view.get(_Request(), operation_id, "readback")
    assert wrong_resource.status == 409
    assert _payload(wrong_resource) == {"error_code": "operation_readback_mismatch"}
    production.config = original_config
    operation_record["production_config_sha256"] = original_operation_sha

    asset_path = (
        tmp_path
        / "www"
        / "aurora"
        / "revisions"
        / transaction["active_dashboard_asset"]
    )
    original_asset = asset_path.read_bytes()
    asset_path.unlink()
    missing_asset = await operation_view.get(_Request(), operation_id, "readback")
    assert missing_asset.status == 409
    assert _payload(missing_asset) == {"error_code": "operation_readback_mismatch"}
    asset_path.write_bytes(original_asset)

    asset_path.write_bytes(b"tampered-dashboard-resource")
    tampered_asset = await operation_view.get(_Request(), operation_id, "readback")
    assert tampered_asset.status == 409
    assert _payload(tampered_asset) == {"error_code": "operation_readback_mismatch"}
    asset_path.write_bytes(original_asset)
    assert production.async_save.await_count == 1

    retried = await view.post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "operation_id": receipt["operation_id"],
                "expected_production_revision": inspection["production_revision"],
                "receipt": receipt,
            }
        ),
        "promote-home-command",
    )
    assert retried.status == 200
    assert _payload(retried)["idempotent"] is True

    collision_receipt = _v2_receipt(
        inspection, now, private, nonce="validation-v2-collision-001"
    )
    collision_receipt["operation_id"] = operation_id
    collision_receipt = _sign_receipt(collision_receipt, private)
    collision = await view.post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "operation_id": collision_receipt["operation_id"],
                "expected_production_revision": inspection["production_revision"],
                "receipt": collision_receipt,
            }
        ),
        "promote-home-command",
    )
    assert collision.status == 409
    assert _payload(collision) == {"error_code": "operation_id_conflict"}

    replay_receipt = json.loads(json.dumps(receipt))
    replay_receipt["operation_id"] = "operation-replay-new-001"
    replay_receipt = _sign_receipt(replay_receipt, private)
    replay = await view.post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "operation_id": replay_receipt["operation_id"],
                "expected_production_revision": inspection["production_revision"],
                "receipt": replay_receipt,
            }
        ),
        "promote-home-command",
    )
    assert replay.status == 409
    assert _payload(replay) == {"error_code": "validation_receipt_replay"}


@pytest.mark.asyncio
async def test_v2_ed25519_receipt_tamper_and_cas_fail_closed(tmp_path, monkeypatch):
    state, transaction, _preview, _production, now = _promotion_context(
        tmp_path, monkeypatch, "v2-failures"
    )
    private = _validation_signer(state)
    view = adapter.RootView(state.hass, state)
    inspected = await view.post(
        _Request({"preview_revision": transaction["revision"], "inspect": True}),
        "promote-home-command",
    )
    inspection = _payload(inspected)
    receipt = _v2_receipt(inspection, now, private, nonce="validation-v2-tamper-001")
    receipt["profile_results"][0]["screenshot_sha256"] = "f" * 64

    tampered = await view.post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "operation_id": receipt["operation_id"],
                "expected_production_revision": inspection["production_revision"],
                "receipt": receipt,
            }
        ),
        "promote-home-command",
    )
    assert tampered.status == 422
    assert _payload(tampered) == {"error_code": "signature"}

    cas_receipt = _v2_receipt(inspection, now, private, nonce="validation-v2-cas-001")
    state.journal["production_revision"] = "concurrent-production-revision"
    state.save()
    cas_failed = await view.post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "operation_id": cas_receipt["operation_id"],
                "expected_production_revision": inspection["production_revision"],
                "receipt": cas_receipt,
            }
        ),
        "promote-home-command",
    )
    assert cas_failed.status == 409
    assert _payload(cas_failed) == {"error_code": "production_revision_conflict"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "applied"),
    [
        ("promote_home_command", True),
        ("promote_home_command", False),
        ("rollback_home_command", True),
        ("rollback_home_command", False),
    ],
)
async def test_operation_get_reconciles_lost_response_without_duplicate_save(
    tmp_path, monkeypatch, action, applied
):
    old_config = {"views": [{"title": "Old production"}]}
    dashboard_bytes = b"durable-operation-dashboard"
    dashboard_sha = hashlib.sha256(dashboard_bytes).hexdigest()
    asset_name = f"aurora-preview-dashboard-{dashboard_sha}.js"
    resource_url = adapter.DASHBOARD_URL_PREFIX + asset_name
    new_config = {
        "views": [{"title": "New production"}],
        "resources": [{"url": resource_url, "res_type": "module"}],
    }
    operation_id = f"operation-{action}-{int(applied)}"
    journal = {"operations": {}, "transactions": {}}
    if action == "promote_home_command":
        transaction = {
            "transaction_id": "tx-lost-promotion",
            "revision": "revision-new",
            "status": "activated",
            "dashboard_sha256": dashboard_sha,
            "active_dashboard_asset": asset_name,
        }
        operation = {
            "operation_id": operation_id,
            "action": action,
            "status": "prepared",
            "transaction_id": transaction["transaction_id"],
            "preview_revision": transaction["revision"],
            "target_revision": transaction["revision"],
            "expected_production_revision": "revision-old",
            "expected_production_config_sha256": adapter._config_sha256(old_config),
            "preview_config_sha256": adapter._config_sha256(new_config),
            "dashboard_resource_url": resource_url,
            "dashboard_sha256": dashboard_sha,
            "dashboard_size": len(dashboard_bytes),
            "receipt_sha256": "a" * 64,
            "request_sha256": "b" * 64,
        }
        journal.update(
            {
                "transactions": {transaction["transaction_id"]: transaction},
                "production_revision": "revision-old",
                "production_config_sha256": adapter._config_sha256(old_config),
                "active_preview": transaction["revision"],
                "receipt_nonces": {
                    "lost-promotion-nonce-001": transaction["transaction_id"]
                },
                "production_transition": {
                    "operation_id": operation_id,
                    "status": "prepared",
                    "previous": {
                        "revision": "revision-old",
                        "config": old_config,
                        "config_sha256": adapter._config_sha256(old_config),
                    },
                    "next_config": new_config,
                    "next_config_sha256": adapter._config_sha256(new_config),
                    "expected_revision": "revision-old",
                    "expected_config_sha256": adapter._config_sha256(old_config),
                    "to_revision": transaction["revision"],
                    "transaction_id": transaction["transaction_id"],
                    "receipt_nonce": "lost-promotion-nonce-001",
                    "receipt_sha256": "a" * 64,
                    "request_sha256": "b" * 64,
                    "dashboard_resource_url": resource_url,
                    "dashboard_sha256": dashboard_sha,
                    "dashboard_size": len(dashboard_bytes),
                },
            }
        )
        live_config = new_config if applied else old_config
        expected_revision = "revision-new" if applied else "revision-old"
        asset_path = tmp_path / "www" / "aurora" / "revisions" / asset_name
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(dashboard_bytes)
    else:
        operation = {
            "operation_id": operation_id,
            "action": action,
            "status": "prepared",
            "target_revision": "revision-old",
            "expected_production_revision": "revision-new",
            "expected_production_config_sha256": adapter._config_sha256(new_config),
            "request_sha256": "c" * 64,
            "expected_dashboard_resource_url": resource_url,
            "expected_dashboard_sha256": dashboard_sha,
            "expected_dashboard_size": len(dashboard_bytes),
        }
        journal.update(
            {
                "production_revision": "revision-new",
                "production_config_sha256": adapter._config_sha256(new_config),
                "previous_production": {
                    "revision": "revision-old",
                    "config": old_config,
                    "config_sha256": adapter._config_sha256(old_config),
                },
                "rollback_transition": {
                    "operation_id": operation_id,
                    "status": "prepared",
                    "from_revision": "revision-new",
                    "to_revision": "revision-old",
                    "expected_config_sha256": adapter._config_sha256(new_config),
                    "next_config": old_config,
                    "next_config_sha256": adapter._config_sha256(old_config),
                    "request_sha256": "c" * 64,
                    "expected_dashboard_resource_url": resource_url,
                    "expected_dashboard_sha256": dashboard_sha,
                    "expected_dashboard_size": len(dashboard_bytes),
                },
            }
        )
        live_config = old_config if applied else new_config
        expected_revision = "revision-old" if applied else "revision-new"
        asset_path = tmp_path / "www" / "aurora" / "revisions" / asset_name
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(dashboard_bytes)
    journal["operations"][operation_id] = operation
    state = _state(tmp_path, journal)
    production = _Dashboard(live_config)
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (production, production.config)),
    )
    original_save = state.save
    state.save = Mock(wraps=original_save)

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), operation_id, "readback"
    )

    assert response.status == 200
    payload = _payload(response)
    assert payload["status"] == ("committed" if applied else "aborted")
    assert payload["applied"] is applied
    assert payload["verified"] is True
    assert payload["active_revision"] == expected_revision
    assert production.async_save.await_count == 0
    assert state.save.call_count == 1

    repeated = await adapter.TransactionView(state.hass, state).get(
        _Request(), operation_id, "status"
    )
    assert repeated.status == 200
    assert _payload(repeated)["status"] == payload["status"]
    assert state.save.call_count == 1


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
    load_dashboard = AsyncMock(
        side_effect=lambda *_args: (production, production.config)
    )
    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)

    response = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "operation_id": "rollback-operation-route-001",
                "expected_current_revision": "revision-current",
                "expected_current_config_sha256": adapter._config_sha256(
                    current_config
                ),
            }
        ),
        "rollback-home-command",
    )

    assert response.status == 200
    assert _payload(response) == {
        "rolled_back": True,
        "operation_id": "rollback-operation-route-001",
        "status": "committed",
        "active_revision": "revision-prior",
        "production_config_sha256": adapter._config_sha256(prior_config),
        "applied": True,
        "verified": True,
        "dashboard_resource_present": False,
    }
    assert load_dashboard.await_count == 2
    production.async_save.assert_awaited_once_with(prior_config)
    assert state.journal["production_revision"] == "revision-prior"
    assert state.journal["production_config_sha256"] == adapter._config_sha256(
        prior_config
    )
    assert state.journal["previous_production"] is None

    repeated = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "operation_id": "rollback-operation-route-001",
                "expected_current_revision": "revision-current",
                "expected_current_config_sha256": adapter._config_sha256(
                    current_config
                ),
            }
        ),
        "rollback-home-command",
    )
    assert repeated.status == 200
    assert _payload(repeated)["idempotent"] is True

    operation_view = adapter.TransactionView(state.hass, state)
    status = await operation_view.get(
        _Request(), "rollback-operation-route-001", "status"
    )
    assert status.status == 200
    assert _payload(status)["action"] == "rollback_home_command"
    assert _payload(status)["target_revision"] == "revision-prior"
    readback = await operation_view.get(
        _Request(), "rollback-operation-route-001", "readback"
    )
    assert readback.status == 200
    assert _payload(readback)["verified"] is True

    collision = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "operation_id": "rollback-operation-route-001",
                "expected_current_revision": "revision-prior",
                "expected_current_config_sha256": adapter._config_sha256(prior_config),
            }
        ),
        "rollback-home-command",
    )
    assert collision.status == 409
    assert _payload(collision) == {"error_code": "operation_id_conflict"}


@pytest.mark.asyncio
async def test_production_rollback_readback_rehashes_restored_dashboard_asset(
    tmp_path, monkeypatch
):
    current_asset = b"current-production-dashboard"
    current_sha = hashlib.sha256(current_asset).hexdigest()
    current_name = f"aurora-preview-dashboard-{current_sha}.js"
    prior_asset = b"prior-production-dashboard"
    prior_sha = hashlib.sha256(prior_asset).hexdigest()
    prior_name = f"aurora-preview-dashboard-{prior_sha}.js"
    asset_root = tmp_path / "www" / "aurora" / "revisions"
    asset_root.mkdir(parents=True)
    (asset_root / current_name).write_bytes(current_asset)
    prior_path = asset_root / prior_name
    prior_path.write_bytes(prior_asset)
    current_config = adapter._dashboard_config_with_asset(
        {"views": [{"title": "Current production"}]}, current_name
    )
    prior_config = adapter._dashboard_config_with_asset(
        {"views": [{"title": "Prior production"}]}, prior_name
    )
    production = _Dashboard(current_config)
    state = _state(
        tmp_path,
        {
            "production_revision": "revision-current-resource",
            "production_config_sha256": adapter._config_sha256(current_config),
            "previous_production": {
                "revision": "revision-prior-resource",
                "config": prior_config,
                "config_sha256": adapter._config_sha256(prior_config),
            },
        },
    )
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (production, production.config)),
    )
    operation_id = "rollback-operation-resource-001"
    body = {
        "operation_id": operation_id,
        "expected_current_revision": "revision-current-resource",
        "expected_current_config_sha256": adapter._config_sha256(current_config),
    }

    response = await adapter.RootView(state.hass, state).post(
        _Request(body), "rollback-home-command"
    )

    assert response.status == 200
    assert _payload(response)["dashboard_resource_url"] == (
        adapter.DASHBOARD_URL_PREFIX + prior_name
    )
    readback = await adapter.TransactionView(state.hass, state).get(
        _Request(), operation_id, "readback"
    )
    assert readback.status == 200
    assert _payload(readback)["dashboard_sha256"] == prior_sha
    assert _payload(readback)["expected_current_revision"] == (
        "revision-current-resource"
    )

    prior_path.write_bytes(b"tampered-prior-production-dashboard")
    tampered = await adapter.TransactionView(state.hass, state).get(
        _Request(), operation_id, "readback"
    )
    assert tampered.status == 409
    assert _payload(tampered) == {"error_code": "operation_readback_mismatch"}


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_state", ["missing", "tampered"])
async def test_production_rollback_rejects_unverifiable_prior_asset(
    tmp_path, monkeypatch, asset_state
):
    prior_asset = b"prior-production-dashboard"
    prior_sha = hashlib.sha256(prior_asset).hexdigest()
    prior_name = f"aurora-preview-dashboard-{prior_sha}.js"
    prior_config = adapter._dashboard_config_with_asset(
        {"views": [{"title": "Prior production"}]}, prior_name
    )
    prior_path = tmp_path / "www" / "aurora" / "revisions" / prior_name
    prior_path.parent.mkdir(parents=True)
    if asset_state == "tampered":
        prior_path.write_bytes(b"tampered")
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
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (production, production.config)),
    )

    response = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "operation_id": f"rollback-unverifiable-{asset_state}",
                "expected_current_revision": "revision-current",
                "expected_current_config_sha256": adapter._config_sha256(
                    current_config
                ),
            }
        ),
        "rollback-home-command",
    )

    assert response.status == 409
    assert _payload(response) == {
        "error_code": "production_rollback_evidence_invalid"
    }
    production.async_save.assert_not_awaited()


def test_corrupt_journal_fails_closed(tmp_path):
    root = tmp_path / ".storage" / "aurora_deploy_preview"
    root.mkdir(parents=True)
    (root / "journal.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="aurora_deploy_journal_corrupt"):
        adapter.AuroraState._create_sync(_hass(tmp_path))


@pytest.mark.asyncio
async def test_request_rate_limit_fails_closed(tmp_path):
    state = _state(tmp_path)
    state.request_times.extend(
        [adapter.time.monotonic()] * adapter.MAX_REQUESTS_PER_MINUTE
    )

    response = await adapter.RootView(state.hass, state).post(_Request(), "bootstrap")

    assert response.status == 429
    assert _payload(response) == {"error_code": "rate_limited"}


@pytest.mark.asyncio
async def test_transaction_rollback_restores_preview_snapshot(tmp_path, monkeypatch):
    prior_dashboard = b"prior-preview-dashboard"
    prior_dashboard_sha = hashlib.sha256(prior_dashboard).hexdigest()
    prior_asset_name = f"aurora-preview-dashboard-{prior_dashboard_sha}.js"
    prior_asset_path = (
        tmp_path / "www" / "aurora" / "revisions" / prior_asset_name
    )
    prior_asset_path.parent.mkdir(parents=True)
    prior_asset_path.write_bytes(prior_dashboard)
    previous_config = adapter._dashboard_config_with_asset(
        {"views": [{"title": "Before"}]}, prior_asset_name
    )
    current_dashboard_sha = hashlib.sha256(b"current-preview-dashboard").hexdigest()
    current_asset_name = f"aurora-preview-dashboard-{current_dashboard_sha}.js"
    current_config = adapter._dashboard_config_with_asset(
        previous_config, current_asset_name
    )
    prior_transaction = {
        "transaction_id": "tx-prior-preview-rollback",
        "revision": "revision-before-preview",
        "status": "activated",
        "dashboard_sha256": prior_dashboard_sha,
        "active_dashboard_asset": prior_asset_name,
        "active_preview_config_sha256": adapter._config_sha256(previous_config),
    }
    transaction = {
        "transaction_id": "tx-preview-rollback",
        "revision": "revision-preview",
        "status": "activated",
        "dashboard_sha256": current_dashboard_sha,
        "active_dashboard_asset": current_asset_name,
        "preview_before": previous_config,
        "preview_config_sha256_before": adapter._config_sha256(previous_config),
        "active_preview_config_sha256": adapter._config_sha256(current_config),
        "preview_revision_before": "revision-before-preview",
    }
    state = _state(
        tmp_path,
        {
            "transactions": {
                prior_transaction["transaction_id"]: prior_transaction,
                transaction["transaction_id"]: transaction,
            },
            "active_preview": transaction["revision"],
        },
    )
    preview = _Dashboard(current_config)
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(side_effect=lambda *_args: (preview, preview.config)),
    )
    monkeypatch.setattr(
        adapter,
        "_verify_active_transaction",
        AsyncMock(return_value=current_dashboard_sha),
    )

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "rollback"
    )

    assert response.status == 200
    assert _payload(response) == {
        "rolled_back": True,
        "active_revision": "revision-before-preview",
        "previous_revision": "revision-before-preview",
        "preview_active": True,
        "status": "rolled_back",
    }
    preview.async_save.assert_awaited_once_with(transaction["preview_before"])
    assert state.journal["active_preview"] == "revision-before-preview"
    assert transaction["status"] == "rolled_back"

    repeated = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "rollback"
    )
    assert repeated.status == 200
    assert _payload(repeated)["idempotent"] is True
    assert preview.async_save.await_count == 1

    prior_asset_path.write_bytes(b"tampered-prior-preview-dashboard")
    tampered = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "rollback"
    )
    assert tampered.status == 409
    assert _payload(tampered) == {
        "error_code": "preview_rollback_readback_failed"
    }


@pytest.mark.asyncio
async def test_reload_is_verified_dashboard_only_noop(tmp_path, monkeypatch):
    active_config = {"views": [{"title": "Active preview"}]}
    transaction = {
        "transaction_id": "tx-reload",
        "revision": "revision-reload",
        "status": "activated",
        "active_preview_config_sha256": adapter._config_sha256(active_config),
    }
    state = _state(
        tmp_path,
        {
            "transactions": {transaction["transaction_id"]: transaction},
            "active_preview": transaction["revision"],
        },
    )
    verify_active = AsyncMock(return_value="f" * 64)
    monkeypatch.setattr(adapter, "_verify_active_transaction", verify_active)
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(return_value=(_Dashboard(active_config), active_config)),
    )

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "reload"
    )

    assert response.status == 200
    assert _payload(response) == {
        "reloaded": True,
        "verified": True,
        "active_revision": transaction["revision"],
        "previous_revision": None,
        "status": "reloaded",
        "restart_required": False,
        "backend_unchanged": True,
    }
    verify_active.assert_awaited_once_with(state.hass, transaction, state.root)

    repeated = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "reload"
    )
    assert repeated.status == 200
    assert _payload(repeated)["idempotent"] is True
    assert verify_active.await_count == 2
