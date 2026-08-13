"""Red-team regressions for Aurora staged integrity and durable promotion."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.aurora_deploy import adapter


class _Content:
    def __init__(self, body: dict | None = None) -> None:
        self.raw = json.dumps(body or {}).encode()
        self._read = False

    async def read(self, _limit: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self.raw


class _Request(dict):
    def __init__(self, body: dict | None = None) -> None:
        super().__init__(hass_user=SimpleNamespace(is_admin=True))
        self.content = _Content(body)
        self.content_length = len(self.content.raw)


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


def _staged_transaction(tmp_path) -> tuple[dict, dict[str, bytes]]:
    manifest = {"schema_version": 1, "nonce": "redteam-nonce", "signature": "x"}
    artifacts = {
        "manifest.json": adapter._json_bytes(manifest),
        "aurora-preview-package.tar.gz": b"package-bytes",
        "aurora-preview-dashboard.js": b"dashboard-bytes",
    }
    hashes = {
        "manifest_sha256": hashlib.sha256(
            adapter._canonical_manifest(manifest)
        ).hexdigest(),
        "package_sha256": hashlib.sha256(
            artifacts["aurora-preview-package.tar.gz"]
        ).hexdigest(),
        "dashboard_sha256": hashlib.sha256(
            artifacts["aurora-preview-dashboard.js"]
        ).hexdigest(),
    }
    revision = hashlib.sha256(
        (
            hashes["manifest_sha256"]
            + hashes["package_sha256"]
            + hashes["dashboard_sha256"]
        ).encode()
    ).hexdigest()[:32]
    revision_dir = tmp_path / "staged" / revision
    revision_dir.mkdir(parents=True)
    for name, raw in artifacts.items():
        (revision_dir / name).write_bytes(raw)
    transaction = {
        "transaction_id": "tx-redteam",
        "revision": revision,
        "status": "verified",
        "target": adapter.PREVIEW,
        "revision_dir": str(revision_dir),
        **hashes,
    }
    return transaction, artifacts


@pytest.mark.asyncio
async def test_readback_accepts_untouched_staged_artifacts(tmp_path, monkeypatch):
    transaction, _artifacts = _staged_transaction(tmp_path)
    monkeypatch.setattr(adapter, "_verify_signature", Mock())
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 200
    assert json.loads(response.body)["status"] == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artifact_name",
    [
        "manifest.json",
        "aurora-preview-package.tar.gz",
        "aurora-preview-dashboard.js",
    ],
)
async def test_readback_rehashes_every_staged_artifact(tmp_path, artifact_name):
    transaction, _artifacts = _staged_transaction(tmp_path)
    (Path(transaction["revision_dir"]) / artifact_name).write_bytes(b"tampered")
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 409
    assert json.loads(response.body) == {"error_code": "staged_integrity_failed"}


def test_staged_readback_reverifies_release_signature(tmp_path, monkeypatch):
    transaction, _artifacts = _staged_transaction(tmp_path)
    verify_signature = Mock()
    monkeypatch.setattr(adapter, "_verify_signature", verify_signature)

    adapter._verify_staged_artifacts(
        transaction,
        tmp_path,
        _hass(tmp_path),
    )

    verify_signature.assert_called_once()
    passed_hass, document = verify_signature.call_args.args
    assert passed_hass is not None
    assert document["signature"] == "x"
    assert verify_signature.call_args.kwargs == {"prefix": "release-"}


def test_staged_artifact_quota_bounds_revision_count_and_bytes(
    tmp_path, monkeypatch
):
    staged = tmp_path / "staged"
    first = staged / "first-revision"
    first.mkdir(parents=True)
    (first / "manifest.json").write_bytes(b"1234")
    monkeypatch.setattr(adapter, "MAX_STAGED_REVISIONS", 1)
    monkeypatch.setattr(adapter, "MAX_STAGED_TOTAL_BYTES", 6)

    with pytest.raises(ValueError, match="staged_capacity_exceeded"):
        adapter._reserve_staged_capacity(tmp_path, "second-revision", 1)

    monkeypatch.setattr(adapter, "MAX_STAGED_REVISIONS", 2)
    with pytest.raises(ValueError, match="staged_capacity_exceeded"):
        adapter._reserve_staged_capacity(tmp_path, "second-revision", 3)

    adapter._reserve_staged_capacity(tmp_path, "second-revision", 2)


@pytest.mark.asyncio
async def test_readback_rejects_out_of_root_legacy_revision_dir(tmp_path):
    transaction, artifacts = _staged_transaction(tmp_path)
    canonical = Path(transaction["revision_dir"])
    shutil.rmtree(canonical)
    outside = tmp_path / "outside-revision"
    outside.mkdir()
    for name, raw in artifacts.items():
        (outside / name).write_bytes(raw)
    transaction["revision_dir"] = str(outside)
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 409
    assert json.loads(response.body) == {"error_code": "staged_integrity_failed"}


@pytest.mark.asyncio
async def test_readback_rejects_symlinked_staged_root(tmp_path):
    transaction, artifacts = _staged_transaction(tmp_path)
    shutil.rmtree(tmp_path / "staged")
    outside = tmp_path / "outside-staged"
    revision_dir = outside / transaction["revision"]
    revision_dir.mkdir(parents=True)
    for name, raw in artifacts.items():
        (revision_dir / name).write_bytes(raw)
    (tmp_path / "staged").symlink_to(outside, target_is_directory=True)
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 409
    assert json.loads(response.body) == {"error_code": "staged_integrity_failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artifact_name",
    [
        "manifest.json",
        "aurora-preview-package.tar.gz",
        "aurora-preview-dashboard.js",
    ],
)
async def test_readback_rejects_symlinked_staged_artifact(tmp_path, artifact_name):
    transaction, _artifacts = _staged_transaction(tmp_path)
    artifact_path = Path(transaction["revision_dir"]) / artifact_name
    outside = tmp_path / f"outside-{artifact_name.replace('.', '-') }"
    outside.write_bytes(artifact_path.read_bytes())
    artifact_path.unlink()
    artifact_path.symlink_to(outside)
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 409
    assert json.loads(response.body) == {"error_code": "staged_integrity_failed"}


@pytest.mark.asyncio
async def test_malformed_revision_never_falls_back_to_legacy_revision_dir(tmp_path):
    transaction, artifacts = _staged_transaction(tmp_path)
    shutil.rmtree(Path(transaction["revision_dir"]))
    outside = tmp_path / "outside-malformed"
    outside.mkdir()
    for name, raw in artifacts.items():
        (outside / name).write_bytes(raw)
    transaction["revision"] = "../outside-malformed"
    transaction["revision_dir"] = str(outside)
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )

    response = await adapter.TransactionView(state.hass, state).get(
        _Request(), transaction["transaction_id"], "readback"
    )

    assert response.status == 409
    assert json.loads(response.body) == {"error_code": "staged_integrity_failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artifact_name",
    [
        "manifest.json",
        "aurora-preview-package.tar.gz",
        "aurora-preview-dashboard.js",
    ],
)
async def test_activation_rehashes_every_staged_artifact(
    tmp_path, monkeypatch, artifact_name
):
    transaction, _artifacts = _staged_transaction(tmp_path)
    (Path(transaction["revision_dir"]) / artifact_name).write_bytes(b"tampered")
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )
    ensure_preview = AsyncMock()
    monkeypatch.setattr(adapter, "_ensure_preview", ensure_preview)

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "activate"
    )

    assert response.status == 500
    assert json.loads(response.body) == {"error_code": "activation_failed"}
    ensure_preview.assert_not_awaited()


@pytest.mark.asyncio
async def test_activation_path_failure_has_no_side_effects(tmp_path, monkeypatch):
    transaction, _artifacts = _staged_transaction(tmp_path)
    artifact_path = Path(transaction["revision_dir"]) / "aurora-preview-dashboard.js"
    outside = tmp_path / "outside-dashboard.js"
    outside.write_bytes(artifact_path.read_bytes())
    artifact_path.unlink()
    artifact_path.symlink_to(outside)
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {"transactions": {transaction["transaction_id"]: transaction}},
    )
    ensure_preview = AsyncMock()
    save_asset = AsyncMock()
    monkeypatch.setattr(adapter, "_ensure_preview", ensure_preview)
    monkeypatch.setattr(adapter, "_save_preview_asset", save_asset)

    response = await adapter.TransactionView(state.hass, state).post(
        _Request(), transaction["transaction_id"], "activate"
    )

    assert response.status == 500
    assert json.loads(response.body) == {"error_code": "activation_failed"}
    ensure_preview.assert_not_awaited()
    save_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_assets_are_immutable_and_revision_specific(
    tmp_path, monkeypatch
):
    config = {"resources": []}
    dashboard = _Dashboard(config)
    async def save_config(updated):
        config.clear()
        config.update(copy.deepcopy(updated))

    dashboard.async_save = AsyncMock(side_effect=save_config)
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(return_value=(dashboard, config)),
    )
    hass = _hass(tmp_path)

    await adapter._save_preview_asset(hass, b"revision-one")
    first_url = config["resources"][0]["url"]
    first_path = tmp_path / "www" / first_url.removeprefix("/local/")

    await adapter._save_preview_asset(hass, b"revision-two")
    second_url = config["resources"][0]["url"]
    second_path = tmp_path / "www" / second_url.removeprefix("/local/")

    assert first_url != second_url
    assert first_path.read_bytes() == b"revision-one"
    assert second_path.read_bytes() == b"revision-two"


def _promotion_fixture(tmp_path):
    revision = "revision-promote"
    transaction = {
        "transaction_id": "tx-promote",
        "revision": revision,
        "status": "activated",
        "manifest_sha256": "a" * 64,
        "package_sha256": "b" * 64,
        "dashboard_sha256": "c" * 64,
    }
    state = adapter.AuroraState(
        _hass(tmp_path),
        tmp_path,
        {
            "transactions": {"tx-promote": transaction},
            "active_preview": revision,
            "production_revision": "revision-before",
        },
    )
    receipt = {
        "schema_version": 1,
        "preview_revision": revision,
        "dashboard_target": adapter.PREVIEW,
        "physical_validation": True,
        "issued_at": datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
        "nonce": "validation-nonce-redteam-001",
        "signer": "validation-redteam",
        "signature": "fixture-signature",
        "expected_production_revision": "revision-before",
        "preview_config_sha256": "d" * 64,
        "expected_production_config_sha256": "e" * 64,
        "device_results": [
            {"device_id": device, "passed": True}
            for device in ("mobile", "kiosk", "tablet", "laptop", "desktop")
        ],
        "expires_at": (
            datetime(2030, 1, 1, tzinfo=UTC) + timedelta(hours=1)
        ).isoformat(),
        "manifest_sha256": transaction["manifest_sha256"],
        "package_sha256": transaction["package_sha256"],
        "dashboard_sha256": transaction["dashboard_sha256"],
    }
    return state, transaction, receipt


def _bind_receipt_configs(receipt, preview_config, production_config):
    dashboard_sha = receipt["dashboard_sha256"]
    preview_config["resources"] = [
        {
            "url": (
                adapter.DASHBOARD_URL_PREFIX
                + f"aurora-preview-dashboard-{dashboard_sha}.js"
            ),
            "res_type": "module",
        }
    ]
    receipt["preview_config_sha256"] = adapter._config_sha256(preview_config)
    receipt["expected_production_config_sha256"] = adapter._config_sha256(
        production_config
    )


@pytest.mark.asyncio
async def test_promotion_journals_prepared_state_before_dashboard_save(
    tmp_path, monkeypatch
):
    state, transaction, receipt = _promotion_fixture(tmp_path)
    preview_config = {"views": [{"title": "Preview"}]}
    production_config = {"views": [{"title": "Production"}]}
    preview = _Dashboard(preview_config)
    production = _Dashboard(production_config)

    async def save_production(next_config):
        production_config.clear()
        production_config.update(copy.deepcopy(next_config))

    production.async_save = AsyncMock(side_effect=save_production)
    _bind_receipt_configs(receipt, preview_config, production_config)
    snapshots = []
    state.save = Mock(side_effect=lambda: snapshots.append(copy.deepcopy(state.journal)))

    async def load_dashboard(_hass, url_path):
        return (
            (preview, preview_config)
            if url_path == adapter.PREVIEW
            else (production, production_config)
        )

    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)
    monkeypatch.setattr(adapter, "_verify_signature", Mock())
    monkeypatch.setattr(adapter, "_verify_active_transaction", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_active_resource_context",
        Mock(
            return_value={
                "preview_resource_url": (
                    adapter.DASHBOARD_URL_PREFIX
                    + f"aurora-preview-dashboard-{transaction['dashboard_sha256']}.js"
                ),
                "preview_resource_sha256": transaction["dashboard_sha256"],
                "preview_resource_size": 123,
            }
        ),
    )
    monkeypatch.setattr(
        adapter, "_now", lambda: datetime(2030, 1, 1, tzinfo=UTC)
    )

    response = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "expected_production_revision": "revision-before",
                "operation_id": "018f3f77-4d52-4cd2-9ce0-b9e9b547b101",
                "receipt": receipt,
            }
        ),
        "promote-home-command",
    )

    assert response.status == 200
    prepared = next(
        snapshot
        for snapshot in snapshots
        if snapshot.get("production_transition", {}).get("status") == "prepared"
    )
    assert prepared["production_revision"] == "revision-before"
    assert snapshots[-1]["production_transition"]["status"] == "committed"
    assert snapshots[-1]["production_revision"] == transaction["revision"]


@pytest.mark.asyncio
async def test_promotion_cas_rejects_stale_expected_revision(tmp_path, monkeypatch):
    state, transaction, receipt = _promotion_fixture(tmp_path)
    preview_config = {"views": [{"title": "Preview"}]}
    production_config = {"views": [{"title": "Production"}]}
    _bind_receipt_configs(receipt, preview_config, production_config)
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(
            side_effect=[
                (_Dashboard(preview_config), preview_config),
                (_Dashboard(production_config), production_config),
            ]
        ),
    )
    monkeypatch.setattr(adapter, "_verify_signature", Mock())
    monkeypatch.setattr(adapter, "_verify_active_transaction", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_active_resource_context",
        Mock(
            return_value={
                "preview_resource_url": (
                    adapter.DASHBOARD_URL_PREFIX
                    + f"aurora-preview-dashboard-{transaction['dashboard_sha256']}.js"
                ),
                "preview_resource_sha256": transaction["dashboard_sha256"],
                "preview_resource_size": 123,
            }
        ),
    )
    monkeypatch.setattr(
        adapter, "_now", lambda: datetime(2030, 1, 1, tzinfo=UTC)
    )

    response = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "expected_production_revision": "stale-revision",
                "operation_id": "018f3f77-4d52-4cd2-9ce0-b9e9b547b102",
                "receipt": {**receipt, "expected_production_revision": "stale-revision"},
            }
        ),
        "promote-home-command",
    )

    assert response.status == 409
    assert json.loads(response.body) == {
        "error_code": "production_revision_conflict"
    }
    assert "production_transition" not in state.journal


@pytest.mark.asyncio
async def test_failed_dashboard_save_leaves_durable_prepared_transition(
    tmp_path, monkeypatch
):
    state, transaction, receipt = _promotion_fixture(tmp_path)
    preview_config = {"views": [{"title": "Preview"}]}
    production_config = {"views": [{"title": "Production"}]}
    _bind_receipt_configs(receipt, preview_config, production_config)
    production = _Dashboard(production_config)
    production.async_save = AsyncMock(side_effect=RuntimeError("storage failure"))

    async def load_dashboard(_hass, url_path):
        if url_path == adapter.PREVIEW:
            return _Dashboard(preview_config), preview_config
        return production, production_config

    monkeypatch.setattr(adapter, "_load_dashboard", load_dashboard)
    monkeypatch.setattr(adapter, "_verify_signature", Mock())
    monkeypatch.setattr(adapter, "_verify_active_transaction", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_active_resource_context",
        Mock(
            return_value={
                "preview_resource_url": (
                    adapter.DASHBOARD_URL_PREFIX
                    + f"aurora-preview-dashboard-{transaction['dashboard_sha256']}.js"
                ),
                "preview_resource_sha256": transaction["dashboard_sha256"],
                "preview_resource_size": 123,
            }
        ),
    )
    monkeypatch.setattr(
        adapter, "_now", lambda: datetime(2030, 1, 1, tzinfo=UTC)
    )

    response = await adapter.RootView(state.hass, state).post(
        _Request(
            {
                "preview_revision": transaction["revision"],
                "expected_production_revision": "revision-before",
                "operation_id": "018f3f77-4d52-4cd2-9ce0-b9e9b547b103",
                "receipt": receipt,
            }
        ),
        "promote-home-command",
    )

    assert response.status == 500
    assert state.journal["production_transition"]["status"] == "prepared"
    assert state.journal["production_revision"] == "revision-before"
    assert transaction["status"] == "activated"


@pytest.mark.asyncio
async def test_prepared_transition_recovers_committed_live_config(
    tmp_path, monkeypatch
):
    state, transaction, _receipt = _promotion_fixture(tmp_path)
    previous_config = {"views": [{"title": "Before"}]}
    dashboard_bytes = b"recovered-dashboard"
    dashboard_sha = hashlib.sha256(dashboard_bytes).hexdigest()
    asset_name = f"aurora-preview-dashboard-{dashboard_sha}.js"
    resource_url = adapter.DASHBOARD_URL_PREFIX + asset_name
    asset_path = tmp_path / "www" / "aurora" / "revisions" / asset_name
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(dashboard_bytes)
    next_config = {
        "views": [{"title": "After"}],
        "resources": [{"url": resource_url, "res_type": "module"}],
    }
    previous_sha = adapter._config_sha256(previous_config)
    next_sha = adapter._config_sha256(next_config)
    receipt_sha = "1" * 64
    request_sha = "2" * 64
    transaction["dashboard_sha256"] = dashboard_sha
    transaction["active_dashboard_asset"] = asset_name
    state.journal["production_config_sha256"] = previous_sha
    state.journal["receipt_nonces"] = {
        "validation-recovery-nonce": transaction["transaction_id"]
    }
    state.journal["production_transition"] = {
        "transition_id": "promotion-recovery",
        "operation_id": "operation-recovery",
        "status": "prepared",
        "expected_revision": "revision-before",
        "expected_config_sha256": previous_sha,
        "to_revision": transaction["revision"],
        "transaction_id": transaction["transaction_id"],
        "receipt_nonce": "validation-recovery-nonce",
        "receipt_sha256": receipt_sha,
        "request_sha256": request_sha,
        "previous": {
            "revision": "revision-before",
            "config": previous_config,
            "config_sha256": previous_sha,
        },
        "next_config": next_config,
        "next_config_sha256": next_sha,
        "dashboard_resource_url": resource_url,
        "dashboard_sha256": dashboard_sha,
        "dashboard_size": len(dashboard_bytes),
    }
    state.journal["operations"] = {
        "operation-recovery": {
            "operation_id": "operation-recovery",
            "action": "promote_home_command",
            "status": "prepared",
            "transaction_id": transaction["transaction_id"],
            "preview_revision": transaction["revision"],
            "target_revision": transaction["revision"],
            "expected_production_revision": "revision-before",
            "expected_production_config_sha256": previous_sha,
            "preview_config_sha256": next_sha,
            "receipt_sha256": receipt_sha,
            "request_sha256": request_sha,
            "dashboard_resource_url": resource_url,
            "dashboard_sha256": dashboard_sha,
            "dashboard_size": len(dashboard_bytes),
        }
    }
    state.journal["production_recovery_required"] = True
    monkeypatch.setattr(
        adapter,
        "_load_dashboard",
        AsyncMock(return_value=(_Dashboard(next_config), next_config)),
    )

    await adapter._reconcile_production_transition(state.hass, state)

    assert state.journal["production_transition"]["status"] == "committed"
    assert state.journal["production_transition"]["recovered"] is True
    assert state.journal["production_revision"] == transaction["revision"]
    assert state.journal["production_config_sha256"] == adapter._config_sha256(
        next_config
    )
    assert transaction["status"] == "promoted"
    assert "production_recovery_required" not in state.journal
