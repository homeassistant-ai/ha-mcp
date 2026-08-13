"""Failure-oriented tests for the Aurora File editor bootstrap."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import bootstrap_aurora_deploy_via_file_editor as bootstrap


@pytest.fixture(autouse=True)
def _fixed_approved_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "APPROVED_STATE_DIRECTORY",
        tmp_path / "approved-main-repository" / "local" / "aurora-deploy-state",
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    component = root / "custom_components" / "aurora_deploy"
    component.mkdir(parents=True)
    (component / "__init__.py").write_text("DOMAIN = 'aurora_deploy'\n")
    (component / "adapter.py").write_text("SAFE = True\n")
    (component / "manifest.json").write_text(
        '{"domain":"aurora_deploy","version":"1.0.0"}\n'
    )
    keys = {
        "release-test": base64.b64encode(b"r" * 32).decode(),
        "validation-test": base64.b64encode(b"v" * 32).decode(),
    }
    (root / bootstrap.TRUST_PATH).write_text(json.dumps(keys, sort_keys=True))
    (root / ".gitignore").write_text("local/\n")
    _git(root, "init", "-q")
    _git(root, "add", ".gitignore", "custom_components", bootstrap.TRUST_PATH)
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return root


def _pins(root: Path) -> dict[str, str]:
    revision = _git(root, "rev-parse", "HEAD")
    files = []
    for relative in bootstrap.PAYLOAD_PATHS:
        content = (root / relative).read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    release = hashlib.sha256(b"r" * 32).hexdigest()
    validation = hashlib.sha256(b"v" * 32).hexdigest()
    manifest = bootstrap._canonical(
        {
            "schemaVersion": "aurora-deploy-bootstrap-v2",
            "component": "aurora_deploy",
            "configurationKey": "aurora_deploy",
            "sourceRevision": revision,
            "files": files,
            "trust": {
                "releaseKeySha256": release,
                "validationKeySha256": validation,
            },
        }
    )
    return {
        "expected_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "expected_release_key_sha256": release,
        "expected_validation_key_sha256": validation,
        "expected_source_revision": revision,
    }


def _payload(root: Path) -> bootstrap.Payload:
    return bootstrap.validate_local_payload(root, **_pins(root))


def _file_editor_client() -> bootstrap.FileEditorIngressClient:
    return bootstrap.FileEditorIngressClient(
        "https://ha.example.test",
        "/api/hassio_ingress/file-editor-test",
        "fixed-test-session",
    )


def _test_credentials() -> tuple[str, str]:
    return "https://ha.example.test", "test-secret-token"


def _local_record_value(
    txid: str,
    *,
    installer: bytes | None = None,
    replace_exact: bool = False,
) -> dict[str, Any]:
    installer = bootstrap.INSTALLER_SOURCE if installer is None else installer
    return {
        "schemaVersion": "aurora-deploy-bootstrap-local-v1",
        "status": "prepared",
        "transactionId": txid,
        "payloadManifestSha256": "2" * 64,
        "sourceRevision": "3" * 40,
        "sourceRoot": "/removed/feature-worktree",
        "installerSha256": hashlib.sha256(installer).hexdigest(),
        "installerSourceBase64": base64.b64encode(installer).decode(),
        "replaceExact": replace_exact,
        "expectedConfigurationSha256": "0" * 64,
        "expectedComponentTreeSha256": "1" * 64,
        "expectedTrustSha256": "absent",
    }


def test_credentials_only_from_root_env_and_http_only_for_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = _source_root(tmp_path / "feature-worktree")
    repository_root = tmp_path / "main-repository"
    repository_root.mkdir()
    root_env = repository_root / ".env"
    root_env.write_text(
        "HOMEASSISTANT_URL=https://ha.example.test\n"
        "HOMEASSISTANT_TOKEN=test-secret-token\n"
    )
    root_env.chmod(0o600)
    (source_root / ".env").write_text(
        "HOMEASSISTANT_URL=https://attacker.invalid\n"
        "HOMEASSISTANT_TOKEN=worktree-copy\n"
    )
    (source_root / ".env").chmod(0o600)
    (repository_root / ".env.local").write_text(
        "HOMEASSISTANT_URL=https://override.invalid\n"
        "HOMEASSISTANT_TOKEN=override-copy\n"
    )
    monkeypatch.setattr(bootstrap, "APPROVED_REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(bootstrap, "APPROVED_CREDENTIAL_ENV", root_env)

    assert source_root != repository_root
    assert bootstrap._load_credentials() == (
        "https://ha.example.test",
        "test-secret-token",
    )
    assert bootstrap._validate_url("http://127.0.0.1:8123") == "http://127.0.0.1:8123"
    with pytest.raises(bootstrap.BootstrapError, match="https_required"):
        bootstrap._validate_url("http://ha.example.test:8123")
    root_env.write_text(
        "HOMEASSISTANT_TOKEN=a\nHOMEASSISTANT_TOKEN=b\nHOMEASSISTANT_URL=https://ha\n"
    )
    with pytest.raises(bootstrap.BootstrapError, match="duplicate"):
        bootstrap._load_credentials()

    root_env.write_text(
        "HOMEASSISTANT_URL=https://ha.example.test\n"
        "HOMEASSISTANT_TOKEN=test-secret-token\n"
    )
    root_env.chmod(0o644)
    with pytest.raises(bootstrap.BootstrapError, match="permissions_unsafe"):
        bootstrap._load_credentials()
    root_env.chmod(0o400)
    with pytest.raises(bootstrap.BootstrapError, match="permissions_unsafe"):
        bootstrap._load_credentials()

    root_env.unlink()
    credential_copy = repository_root / "credential-copy"
    credential_copy.write_text(
        "HOMEASSISTANT_URL=https://ha.example.test\n"
        "HOMEASSISTANT_TOKEN=test-secret-token\n"
    )
    credential_copy.chmod(0o600)
    root_env.symlink_to(credential_copy)
    with pytest.raises(bootstrap.BootstrapError, match="root_env_invalid"):
        bootstrap._load_credentials()


@pytest.mark.asyncio
async def test_credential_diagnostic_is_transport_only_and_fail_closed(
    tmp_path: Path,
) -> None:
    result = await bootstrap.bootstrap(
        mode="credential-check",
        source_root=tmp_path / "unused-feature-worktree",
        credential_loader=_test_credentials,
    )
    assert result == {
        "status": "credential_transport_ready",
        "credentialSource": "approved_root_env",
        "transport": "https",
    }
    serialized = json.dumps(result)
    assert "ha.example.test" not in serialized
    assert "test-secret-token" not in serialized

    with pytest.raises(bootstrap.BootstrapError, match="https_required"):
        await bootstrap.bootstrap(
            mode="credential-check",
            source_root=tmp_path / "unused-feature-worktree",
            credential_loader=lambda: (
                "http://non-loopback.example.test:8123",
                "must-not-leak",
            ),
        )


def test_committed_source_and_independent_hashes_are_mandatory(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    pins = _pins(root)
    payload = bootstrap.validate_local_payload(root, **pins)
    assert payload.source_revision == pins["expected_source_revision"]

    bad = {**pins, "expected_manifest_sha256": "0" * 64}
    with pytest.raises(bootstrap.BootstrapError, match="manifest_hash_mismatch"):
        bootstrap.validate_local_payload(root, **bad)
    (root / "custom_components/aurora_deploy/adapter.py").write_text("DRIFT = True\n")
    with pytest.raises(bootstrap.BootstrapError, match="not_clean"):
        bootstrap.validate_local_payload(root, **pins)


def test_duplicate_json_keys_and_bad_trust_roles_fail_closed(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    trust = root / bootstrap.TRUST_PATH
    trust.write_text('{"release-a":"x","release-a":"y","validation-a":"z"}')
    _git(root, "add", bootstrap.TRUST_PATH)
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "duplicate",
    )
    with pytest.raises(bootstrap.BootstrapError, match="duplicate"):
        bootstrap.validate_local_payload(root, **_pins(root))

    duplicate = base64.b64encode(b"a" * 32).decode()
    with pytest.raises(bootstrap.BootstrapError, match="not_distinct"):
        bootstrap.validate_trust_store(
            json.dumps({"release-a": duplicate, "validation-a": duplicate}).encode()
        )


@pytest.mark.parametrize("too_long_role", ("release", "validation"))
def test_trust_key_ids_match_adapter_total_length_contract(
    too_long_role: str,
) -> None:
    release_id = "release-" + "r" * (64 - len("release-"))
    validation_id = "validation-" + "v" * (64 - len("validation-"))
    valid = {
        release_id: base64.b64encode(b"r" * 32).decode(),
        validation_id: base64.b64encode(b"v" * 32).decode(),
    }
    bootstrap.validate_trust_store(bootstrap._canonical(valid))

    invalid = dict(valid)
    key = release_id if too_long_role == "release" else validation_id
    invalid[f"{key}x"] = invalid.pop(key)
    with pytest.raises(bootstrap.BootstrapError, match="trust_store_invalid"):
        bootstrap.validate_trust_store(bootstrap._canonical(invalid))


def test_broken_source_and_destination_symlinks_are_rejected(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    adapter = root / "custom_components/aurora_deploy/adapter.py"
    adapter.unlink()
    adapter.symlink_to(root / "missing.py")
    _git(root, "add", "custom_components/aurora_deploy/adapter.py")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "broken-link",
    )
    broken_pins = {
        "expected_manifest_sha256": "0" * 64,
        "expected_release_key_sha256": "1" * 64,
        "expected_validation_key_sha256": "2" * 64,
        "expected_source_revision": _git(root, "rev-parse", "HEAD"),
    }
    with pytest.raises(bootstrap.BootstrapError, match="source_"):
        bootstrap.validate_local_payload(root, **broken_pins)

    safe_root = _source_root(tmp_path / "safe")
    remote, txid, payload = _prepare_remote(tmp_path / "link", safe_root)
    (remote / "custom_components/aurora_deploy").symlink_to(remote / "missing")
    result = _run(remote, txid, payload, "install")
    assert result.returncode == 1
    assert "destination_component_invalid" in result.stderr


def _preflight_values(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    return {
        "user": {"is_admin": True},
        "supervisor": {
            "healthy": True,
            "supported": True,
            "arch": "amd64",
        },
        "host": {
            "deployment": "production",
            "operating_system": "Home Assistant OS 18.2",
            "agent_version": "1.10.0",
            "features": [
                "reboot",
                "shutdown",
                "services",
                "network",
                "hostname",
                "timedate",
                "os_agent",
                "haos",
                "resolved",
                "journal",
                "disk",
                "mount",
            ],
        },
        "backup": {
            "state": "idle",
            "failed_agent_ids": [],
            "agent_errors": {},
            "backups": [
                {
                    "backup_id": "backup-1",
                    "name": "Aurora HA recovery backup",
                    "date": (current - timedelta(minutes=5)).isoformat(),
                    "database_included": True,
                    "homeassistant_included": True,
                    "folders": ["ssl", "media", "share"],
                    "addons": ["core_configurator"],
                    "failed_agent_ids": [],
                    "agent_errors": {},
                    "agents": {
                        "hassio.local": {
                            "protected": True,
                            "size": 1024.5,
                            "status": "available",
                        }
                    },
                }
            ],
        },
        "addon": {
            "slug": bootstrap.FILE_EDITOR_SLUG,
            "name": "File editor",
            "version": bootstrap.FILE_EDITOR_VERSION,
            "state": "stopped",
            "protected": True,
            "ingress": True,
            "ingress_entry": "/api/hassio_ingress/fixed-session",
            "host_network": False,
            "network": None,
            "repository": "core",
        },
        "store": {
            "slug": bootstrap.FILE_EDITOR_SLUG,
            "name": "File editor",
            "version_latest": bootstrap.FILE_EDITOR_VERSION,
            "repository": "core",
            "ingress": True,
            "network": None,
            "arch": ["amd64"],
            "available": True,
        },
        "backup_id": "backup-1",
        "expected_backup_agent_id": bootstrap.APPROVED_BACKUP_AGENT_ID,
        "require_backup": True,
        "now": current,
    }


def test_current_live_haos_2026_8_1_supervisor_host_schema_is_accepted() -> None:
    values = _preflight_values()
    # These retain the security-significant shape observed from the raw live
    # endpoints: Supervisor omits state; Host omits both architecture fields.
    assert "state" not in values["supervisor"]
    assert {"arch", "architecture"}.isdisjoint(values["host"])
    assert values["host"]["agent_version"] == "1.10.0"
    assert values["host"]["features"] == [
        "reboot",
        "shutdown",
        "services",
        "network",
        "hostname",
        "timedate",
        "os_agent",
        "haos",
        "resolved",
        "journal",
        "disk",
        "mount",
    ]
    assert values["addon"]["network"] is None
    assert values["store"]["network"] is None
    assert "map" not in values["addon"]
    assert "map" not in values["store"]
    assert bootstrap.validate_supervisor_preflight(**values).startswith("/api/")


def test_present_supervisor_state_and_architecture_remain_fail_closed() -> None:
    wrong_state = _preflight_values()
    wrong_state["supervisor"]["state"] = "degraded"
    with pytest.raises(bootstrap.BootstrapError, match="supervisor_not_ready"):
        bootstrap.validate_supervisor_preflight(**wrong_state)

    null_state = _preflight_values()
    null_state["supervisor"]["state"] = None
    with pytest.raises(bootstrap.BootstrapError, match="supervisor_not_ready"):
        bootstrap.validate_supervisor_preflight(**null_state)

    wrong_arch = _preflight_values()
    wrong_arch["supervisor"]["arch"] = "aarch64"
    with pytest.raises(bootstrap.BootstrapError, match="unsupported_architecture"):
        bootstrap.validate_supervisor_preflight(**wrong_arch)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("deployment", "development", "host_metadata_invalid"),
        ("operating_system", "Ubuntu 26.04", "host_metadata_invalid"),
        ("operating_system", "Home Assistant OS ", "host_metadata_invalid"),
        (
            "operating_system",
            f"Home Assistant OS 18.2{'a' * 64}",
            "host_metadata_invalid",
        ),
        ("agent_version", "", "host_metadata_invalid"),
        ("agent_version", " " * 65, "host_metadata_invalid"),
        ("features", ["haos"], "host_metadata_invalid"),
        ("features", ["os_agent"], "host_metadata_invalid"),
        ("features", ["haos", "os_agent", 1], "host_metadata_invalid"),
        ("arch", "aarch64", "unsupported_architecture"),
        ("architecture", "armv7", "unsupported_architecture"),
    ),
)
def test_host_identity_fallback_rejects_fake_or_contradictory_metadata(
    field: str, value: Any, error: str
) -> None:
    values = _preflight_values()
    values["host"][field] = value
    with pytest.raises(bootstrap.BootstrapError, match=error):
        bootstrap.validate_supervisor_preflight(**values)


@pytest.mark.parametrize("field", ("arch", "architecture"))
def test_present_amd64_host_architecture_is_accepted_as_authoritative(
    field: str,
) -> None:
    values = _preflight_values()
    values["host"] = {field: "amd64"}
    assert bootstrap.validate_supervisor_preflight(**values).startswith("/api/")


def test_absent_file_editor_network_fields_are_accepted() -> None:
    values = _preflight_values()
    values["addon"].pop("network")
    values["store"].pop("network")
    assert bootstrap.validate_supervisor_preflight(**values).startswith("/api/")


@pytest.mark.parametrize(
    ("section", "network"),
    (
        ("addon", {}),
        ("store", {}),
        ("addon", {"3218/tcp": 3218}),
        ("store", {"3218/tcp": 3218}),
    ),
)
def test_present_file_editor_network_mapping_is_rejected(
    section: str, network: dict[str, int]
) -> None:
    values = _preflight_values()
    values[section]["network"] = network
    with pytest.raises(bootstrap.BootstrapError, match="file_editor_network_exposed"):
        bootstrap.validate_supervisor_preflight(**values)


def test_preflight_requires_explicit_safe_supervisor_and_strong_backup_metadata() -> (
    None
):
    values = _preflight_values()
    assert bootstrap.validate_supervisor_preflight(**values).startswith("/api/")
    mutations = (
        ("supervisor", "state", "degraded"),
        ("addon", "name", "Impostor"),
        ("addon", "host_network", True),
        ("store", "repository", "third-party"),
        ("backup_item", "database_included", False),
        ("backup_item", "agents", {}),
        (
            "backup_item",
            "agents",
            {
                "hassio.local": {"protected": True, "size": 10},
                "cloud.remote": {"protected": True, "size": 10},
            },
        ),
        (
            "backup_item",
            "agents",
            {
                "cloud.remote": {
                    "protected": True,
                    "size": 10,
                    "status": "available",
                }
            },
        ),
        ("backup", "failed_agent_ids", ["hassio.local"]),
        ("backup", "agent_errors", {"hassio.local": "unavailable"}),
    )
    for section, field, value in mutations:
        candidate = _preflight_values()
        target = (
            candidate["backup"]["backups"][0]
            if section == "backup_item"
            else candidate[section]
        )
        target[field] = value
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.validate_supervisor_preflight(**candidate)


def test_reconstructed_live_c5168fb8_backup_schema_is_accepted() -> None:
    now = datetime(2026, 8, 11, 9, 30, tzinfo=UTC)
    # Reconstructed from the observed backup/info schema. The ID and field layout
    # are provenance-backed; name, timestamp, and size are sanitized test values.
    live_shape = {
        "backup_id": "c5168fb8",
        "name": "Full backup 2026-08-11",
        "date": "2026-08-11T08:45:00+00:00",
        "addons": ["core_configurator", "a0d7b954_vscode"],
        "folders": ["addons/local", "media", "share", "ssl"],
        "homeassistant_included": True,
        "database_included": True,
        "failed_agent_ids": [],
        "agent_errors": {},
        "agents": {
            "hassio.local": {
                "protected": True,
                "size": 1_284_892_160,
            }
        },
    }
    assert (
        bootstrap._fresh_ha_recovery_backup(
            [live_shape],
            "c5168fb8",
            bootstrap.APPROVED_BACKUP_AGENT_ID,
            now=now,
        )
        == live_shape
    )


@pytest.mark.parametrize(
    "agent",
    (
        {"protected": False, "size": 1024, "status": "available"},
        {"protected": True, "size": 0, "status": "available"},
        {"protected": True, "size": float("inf"), "status": "available"},
        {"protected": True, "size": True, "status": "available"},
        {"protected": True, "size": 1024, "status": "failed"},
    ),
)
def test_backup_rejects_unhealthy_or_inaccessible_agent_copy(
    agent: dict[str, Any],
) -> None:
    values = _preflight_values()
    values["backup"]["backups"][0]["agents"] = {"hassio.local": agent}
    with pytest.raises(bootstrap.BootstrapError, match="protected_ha_recovery_backup"):
        bootstrap.validate_supervisor_preflight(**values)


def _empty_component_digest() -> str:
    return hashlib.sha256(
        bootstrap._canonical({"exists": False, "files": {}, "directories": []})
    ).hexdigest()


@pytest.mark.parametrize(
    "record_id", (str(uuid.uuid4()), bootstrap.LIFECYCLE_RECORD_ID)
)
def test_local_record_create_is_atomic_no_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_id: str,
) -> None:
    value = _local_record_value(record_id)
    original_link = os.link
    competitor = b'{"competitor":"first-writer"}'

    def race_link(source: str | Path, destination: str | Path, **kwargs: Any) -> None:
        target = Path(destination)
        target.write_bytes(competitor)
        target.chmod(0o600)
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(bootstrap.os, "link", race_link)
    with pytest.raises(bootstrap.BootstrapError, match="local_transaction_exists"):
        bootstrap._write_local_record(record_id, value, create=True)

    path = bootstrap.APPROVED_STATE_DIRECTORY / f"{record_id}.json"
    assert path.read_bytes() == competitor
    assert not list(bootstrap.APPROVED_STATE_DIRECTORY.glob(f".{record_id}.new-*"))


def test_local_record_loops_partial_writes_and_cleans_owned_temps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    txid = str(uuid.uuid4())
    value = _local_record_value(txid)
    original_write = os.write

    with monkeypatch.context() as patch:
        patch.setattr(
            bootstrap.os,
            "write",
            lambda fd, data: original_write(fd, bytes(data[:3])),
        )
        bootstrap._write_local_record(txid, value, create=True)
    assert bootstrap._read_local_record(txid) == value

    failed_txid = str(uuid.uuid4())
    calls = 0

    def fail_partial(fd: int, data: Any) -> int:
        nonlocal calls
        calls += 1
        if calls > 1:
            return 0
        return original_write(fd, bytes(data[:3]))

    with monkeypatch.context() as patch:
        patch.setattr(bootstrap.os, "write", fail_partial)
        with pytest.raises(bootstrap.BootstrapError, match="local_state_write_failed"):
            bootstrap._write_local_record(
                failed_txid, _local_record_value(failed_txid), create=True
            )
    assert not list(bootstrap.APPROVED_STATE_DIRECTORY.glob(f".{failed_txid}.new-*"))

    residue = (
        bootstrap.APPROVED_STATE_DIRECTORY / f".{failed_txid}.new-2000000000-{'a' * 16}"
    )
    residue.write_bytes(b"dead owned temp")
    residue.chmod(0o600)
    bootstrap._write_local_record(
        failed_txid, _local_record_value(failed_txid), create=True
    )
    assert not residue.exists()


def test_partial_stage_resume_skips_exact_files_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    payload = _payload(_source_root(tmp_path))
    txid = str(uuid.uuid4())

    class StageClient:
        def __init__(self) -> None:
            self.files = {bootstrap.INSTALLER_NAME: bootstrap.INSTALLER_SOURCE}
            self.uploaded: list[str] = []

        def create_stage(self, _txid: str) -> None:
            return None

        def verify_fixed_root_write_capability(self, _txid: str) -> None:
            return None

        def stage_file_exists(self, _txid: str, relative: str) -> bool:
            return relative in self.files

        def download_stage(self, _txid: str, relative: str) -> bytes:
            return self.files[relative]

        def upload(self, _txid: str, relative: str, content: bytes) -> None:
            self.uploaded.append(relative)
            self.files[relative] = content

    client = StageClient()
    bootstrap._stage_payload(client, txid, payload)
    assert bootstrap.INSTALLER_NAME not in client.uploaded
    assert set(client.files) == {
        bootstrap.INSTALLER_NAME,
        *bootstrap.PAYLOAD_PATHS,
        bootstrap.PAYLOAD_MANIFEST_NAME,
    }

    conflict = StageClient()
    conflict.files[bootstrap.SOURCE_PATHS[0]] = b"external-stage-writer"
    with pytest.raises(bootstrap.BootstrapError, match="remote_stage_conflict"):
        bootstrap._stage_payload(conflict, txid, payload)
    assert conflict.files[bootstrap.SOURCE_PATHS[0]] == b"external-stage-writer"
    assert bootstrap.SOURCE_PATHS[1] not in conflict.files


def test_fixed_root_capability_probe_is_read_back_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_editor_client()
    txid = str(uuid.uuid4())
    staged: dict[str, bytes] = {}
    uploaded: list[str] = []
    uploaded_content: list[bytes] = []

    monkeypatch.setattr(
        client,
        "stage_file_exists",
        lambda _txid, relative: relative in staged,
    )
    monkeypatch.setattr(
        client,
        "download_stage",
        lambda _txid, relative: staged[relative],
    )

    def upload(_txid: str, relative: str, content: bytes) -> None:
        uploaded.append(relative)
        uploaded_content.append(content)
        staged[relative] = content

    def request(
        endpoint: str, *, form: dict[str, str], **_kwargs: Any
    ) -> dict[str, Any]:
        assert endpoint == "/api/delete"
        assert form["path"].endswith(f"/{bootstrap.CAPABILITY_PROBE_NAME}")
        del staged[bootstrap.CAPABILITY_PROBE_NAME]
        return {
            "error": False,
            "message": "Deletion successful",
            "path": form["path"],
        }

    monkeypatch.setattr(client, "upload", upload)
    monkeypatch.setattr(client, "_request", request)
    client.verify_fixed_root_write_capability(txid)

    assert uploaded == [bootstrap.CAPABILITY_PROBE_NAME]
    assert staged == {}

    # A crash after marker publication remains transaction-owned and resumable.
    staged[bootstrap.CAPABILITY_PROBE_NAME] = uploaded_content[0]
    client.verify_fixed_root_write_capability(txid)
    assert uploaded == [
        bootstrap.CAPABILITY_PROBE_NAME,
        bootstrap.CAPABILITY_PROBE_NAME,
    ]
    assert staged == {}


def test_missing_fixed_root_write_capability_stops_before_payload_upload(
    tmp_path: Path,
) -> None:
    payload = _payload(_source_root(tmp_path))

    class MissingCapabilityClient:
        payload_upload_attempted = False

        def create_stage(self, _txid: str) -> None:
            return None

        def verify_fixed_root_write_capability(self, _txid: str) -> None:
            raise bootstrap.BootstrapError("fixed_root_write_capability_required")

        def upload(self, *_args: Any) -> None:
            self.payload_upload_attempted = True

    client = MissingCapabilityClient()
    with pytest.raises(
        bootstrap.BootstrapError, match="fixed_root_write_capability_required"
    ):
        bootstrap._stage_payload(client, str(uuid.uuid4()), payload)
    assert client.payload_upload_attempted is False


def test_fixed_root_capability_probe_preserves_foreign_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_editor_client()
    staged = {bootstrap.CAPABILITY_PROBE_NAME: b"foreign-writer"}
    monkeypatch.setattr(
        client,
        "stage_file_exists",
        lambda _txid, relative: relative in staged,
    )
    monkeypatch.setattr(
        client,
        "download_stage",
        lambda _txid, relative: staged[relative],
    )

    with pytest.raises(
        bootstrap.BootstrapError, match="fixed_root_capability_conflict"
    ):
        client.verify_fixed_root_write_capability(str(uuid.uuid4()))
    assert staged == {bootstrap.CAPABILITY_PROBE_NAME: b"foreign-writer"}


def test_upload_accepts_exact_hass_configurator_0_6_0_success_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_editor_client()
    requests: list[Any] = []

    def open_upload(request: Any, _timeout: float = 30) -> bytes:
        requests.append(request)
        return b'{"error":false,"message":"Upload successful"}'

    monkeypatch.setattr(client, "_open", open_upload)
    client.upload(
        str(uuid.uuid4()),
        bootstrap.INSTALLER_NAME,
        bootstrap.INSTALLER_SOURCE,
    )

    assert len(requests) == 1
    assert requests[0].get_method() == "POST"
    assert requests[0].full_url.endswith("/api/upload")


@pytest.mark.parametrize(
    "response",
    [
        {"error": False},
        {"message": "Upload successful"},
        {"error": True, "message": "Upload successful"},
        {"error": 0, "message": "Upload successful"},
        {"error": False, "message": "upload successful"},
        {
            "error": False,
            "message": "Upload successful",
            "path": "/homeassistant/attacker-selected",
        },
        {"error": False, "message": "Upload successful", "extra": None},
    ],
    ids=(
        "missing-message",
        "missing-error",
        "error",
        "non-boolean-error",
        "wrong-message",
        "path",
        "extra",
    ),
)
def test_upload_rejects_nonexact_hass_configurator_response(
    response: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _file_editor_client()
    monkeypatch.setattr(
        client,
        "_open",
        lambda *_args, **_kwargs: json.dumps(response).encode(),
    )

    with pytest.raises(bootstrap.BootstrapError, match="remote_upload_failed"):
        client.upload(
            str(uuid.uuid4()),
            bootstrap.INSTALLER_NAME,
            bootstrap.INSTALLER_SOURCE,
        )


def test_listdir_accepts_upstream_schema_and_binds_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_editor_client()
    response = {
        "content": [],
        "abspath": "/homeassistant",
        "parent": "/",
        "branches": [],
        "activebranch": None,
        "dirty": False,
        "error": None,
    }
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: response)
    assert client._list("/homeassistant") == []

    response["abspath"] = "/attacker-selected"
    with pytest.raises(bootstrap.BootstrapError, match="remote_path_mismatch"):
        client._list("/homeassistant")


def test_configuration_download_requires_listed_fixed_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_editor_client()
    monkeypatch.setattr(client, "_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        client,
        "_download_path",
        lambda *_args: pytest.fail("missing configuration must not be downloaded"),
    )

    with pytest.raises(bootstrap.BootstrapError, match="remote_configuration_missing"):
        client.configuration_bytes()


def test_exec_accepts_complete_hass_configurator_0_6_0_success_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_editor_client()
    txid = str(uuid.uuid4())
    monkeypatch.setattr(
        client,
        "_stage",
        lambda _txid, _relative="": "/homeassistant/stage/installer.py",
    )
    monkeypatch.setattr(
        client, "_download_path", lambda _path: bootstrap.INSTALLER_SOURCE
    )

    def request(
        endpoint: str, *, form: dict[str, str], **_kwargs: Any
    ) -> dict[str, Any]:
        assert endpoint == "/api/exec_command"
        command = form["command"]
        return {
            "error": False,
            "message": f"Command executed: {command}",
            "returncode": 0,
            "stdout": json.dumps({"status": "stage_aborted", "transactionId": txid}),
            "stderr": "",
        }

    monkeypatch.setattr(client, "_request", request)
    assert client.execute(
        mode="abort-stage",
        transaction_id=txid,
        arguments={"expected-installer-sha256": bootstrap.INSTALLER_SHA256},
    ) == {"status": "stage_aborted", "transactionId": txid}


@pytest.mark.parametrize(
    "malformation", ("missing-message", "wrong-message", "boolean-returncode", "extra")
)
def test_exec_malformed_upstream_wrapper_fails_closed(
    malformation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _file_editor_client()
    txid = str(uuid.uuid4())
    monkeypatch.setattr(
        client,
        "_stage",
        lambda _txid, _relative="": "/homeassistant/stage/installer.py",
    )
    monkeypatch.setattr(
        client, "_download_path", lambda _path: bootstrap.INSTALLER_SOURCE
    )

    def request(
        _endpoint: str, *, form: dict[str, str], **_kwargs: Any
    ) -> dict[str, Any]:
        wrapper: dict[str, Any] = {
            "error": False,
            "message": f"Command executed: {form['command']}",
            "returncode": 0,
            "stdout": json.dumps({"status": "stage_aborted", "transactionId": txid}),
            "stderr": "",
        }
        if malformation == "missing-message":
            del wrapper["message"]
        elif malformation == "wrong-message":
            wrapper["message"] = "Command executed: attacker-selected"
        elif malformation == "boolean-returncode":
            wrapper["returncode"] = False
        else:
            wrapper["extra"] = None
        return wrapper

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(
        client,
        "_reconcile_lost_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bootstrap.BootstrapError("installer_result_ambiguous")
        ),
    )
    with pytest.raises(bootstrap.BootstrapError, match="installer_result_ambiguous"):
        client.execute(
            mode="abort-stage",
            transaction_id=txid,
            arguments={"expected-installer-sha256": bootstrap.INSTALLER_SHA256},
        )


def _prepare_remote(tmp_path: Path, root: Path) -> tuple[Path, str, bootstrap.Payload]:
    remote = tmp_path / "remote"
    (remote / "custom_components").mkdir(parents=True)
    (remote / "configuration.yaml").write_bytes(b"default_config:\n")
    txid = str(uuid.uuid4())
    payload = _payload(root)
    _write_stage(remote, txid, payload)
    return remote, txid, payload


def _write_stage(remote: Path, txid: str, payload: bootstrap.Payload) -> Path:
    stage = remote / f"{bootstrap.STAGE_PREFIX}{txid}"
    stage.mkdir()
    for item in payload.files:
        target = stage / item.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)
    (stage / bootstrap.PAYLOAD_MANIFEST_NAME).write_bytes(payload.manifest)
    return stage


def _installer_source(remote: Path, *, failure: str | None = None) -> str:
    source = bootstrap.INSTALLER_SOURCE.decode().replace(
        'ROOT=Path("/homeassistant")', f"ROOT=Path({str(remote)!r})"
    )
    mutations = {
            "post_cas_component": 'COMPONENT.mkdir(); (COMPONENT/"writer.py").write_bytes(b"external-writer")',
            "post_cas_trust": 'TRUST.write_bytes(b"external-writer")',
            "post_cas_config": 'CONFIG.write_bytes(b"external-writer-config\\n")',
    }
    replacements: dict[str, list[tuple[str, str]]] = {
        "after_component": [(
            'install_destination("component",COMPONENT,new_component,tx,current,installed)',
            'install_destination("component",COMPONENT,new_component,tx,current,installed); raise OSError("injected")',
        )],
        **{
            name: [(
            'new_config=tx/"new-configuration.yaml"; assert_expected(states(),args)',
            f'new_config=tx/"new-configuration.yaml"; assert_expected(states(),args); {mutation}',
            )]
            for name, mutation in mutations.items()
        },
        "install_recreate_config": [(
            "publish_verified(key,replacement,destination,installed[key])",
            'if key=="configuration": destination.write_bytes(b"external-recreated-config\\n")\n    publish_verified(key,replacement,destination,installed[key])',
        )],
        "rollback_recreate_config": [(
            'if pre[key]["exists"]: publish_verified(key,previous,destination,pre[key])',
            'if pre[key]["exists"]:\n            if key=="configuration": destination.write_bytes(b"external-rollback-config\\n")\n            publish_verified(key,previous,destination,pre[key])',
        )],
        "lock_hold": [(
            "fsync_dir(ROOT); cleanup_owned_lock_candidate(candidate,created)",
            "fsync_dir(ROOT); cleanup_owned_lock_candidate(candidate,created); time.sleep(1)",
        )],
        "lock_link_failure": [(
            "os.link(candidate,LOCK,follow_symlinks=False); published=True",
            '(_ for _ in ()).throw(OSError("injected-link-failure")); published=True',
        )],
        "lock_partial_write": [(
            'written=os.write(fd,view)\n                if written<=0: fail("global_lock_candidate_write_failed")',
            'written=os.write(fd,view[:1]); fail("global_lock_candidate_write_failed")\n                if written<=0: fail("global_lock_candidate_write_failed")',
        )],
        "runtime_foreign_destination": [
            ('if ROOT!=Path("/homeassistant"): return', "if False: return"),
            ('if sys.platform!="linux": fail("linux_runtime_required")', 'if False: fail("linux_runtime_required")'),
            (
                "created=create_runtime_probe(source,marker); renamed=False",
                'created=create_runtime_probe(source,marker); renamed=False; destination.write_bytes(b"foreign-runtime-writer")',
            ),
        ],
        "runtime_hardlink_failure": [
            ('if ROOT!=Path("/homeassistant"): return', "if False: return"),
            ('if sys.platform!="linux": fail("linux_runtime_required")', 'if False: fail("linux_runtime_required")'),
            (
                "try: os.link(link_source,link_destination,follow_symlinks=False); linked=True",
                'try: (_ for _ in ()).throw(OSError("injected-runtime-link")); linked=True',
            ),
        ],
        "runtime_probe_partial_exit": [
            ('if ROOT!=Path("/homeassistant"): return', "if False: return"),
            ('if sys.platform!="linux": fail("linux_runtime_required")', 'if False: fail("linux_runtime_required")'),
            (
                'written=os.write(fd,view)\n                if written<=0: fail("runtime_probe_write_failed")',
                'written=os.write(fd,view[:1]); os._exit(74)\n                if written<=0: fail("runtime_probe_write_failed")',
            ),
        ],
        **{
            name: [(marker, f"{marker}; os._exit(71)")]
            for name, marker in {
            "init_exit_after_mkdir": "initialization.mkdir(mode=0o700); fsync_dir(ROOT)",
            "init_exit_after_installer": "write_atomic(initialization/INSTALLER,installer_bytes)",
            "init_exit_after_replacements": 'write_atomic(new_config,config_data,current["configuration"])',
            }.items()
        },
        "journal_exit_initial": [(
            "os.replace(temp,path); fsync_dir(path.parent)",
            'if path.name=="transaction.json" and not lexists(path): os._exit(72)\n    os.replace(temp,path); fsync_dir(path.parent)',
        )],
        "journal_exit_later": [(
            "os.replace(temp,path); fsync_dir(path.parent)",
            'marker=ROOT/f".test-later-journal-{args.transaction_id}" if "args" in globals() else ROOT/".test-unused"\n    if path.name=="transaction.json" and lexists(path) and not lexists(marker): marker.write_text("once"); os._exit(73)\n    os.replace(temp,path); fsync_dir(path.parent)',
        )],
        "init_raise_after_replacements": [(
            'write_atomic(new_config,config_data,current["configuration"])',
            'write_atomic(new_config,config_data,current["configuration"]); raise OSError("injected-init")',
        )],
        "config_aba": [(
            "config_state,config_bytes=configuration_snapshot()",
            'config_state,config_bytes=configuration_snapshot(); CONFIG.write_bytes(b"transient-b\\n"); CONFIG.write_bytes(config_bytes)',
        )],
        "finalize_after_prepared": [(
            'if journal.get("status")!="finalize_prepared": journal["status"]="finalize_prepared"; write_journal(tx,journal)',
            'if journal.get("status")!="finalize_prepared": journal["status"]="finalize_prepared"; write_journal(tx,journal); raise OSError("injected_finalize_after_prepared")',
        )],
        "finalize_after_stage": [(
            'if lexists(stage): kind(stage,"dir","stage_invalid"); shutil.rmtree(stage); fsync_dir(ROOT)',
            'if lexists(stage): kind(stage,"dir","stage_invalid"); shutil.rmtree(stage); fsync_dir(ROOT); raise OSError("injected_finalize_after_stage")',
        )],
        "finalize_after_release": [(
            "release(args.transaction_id); release_required=False",
            'release(args.transaction_id); release_required=False; marker=ROOT/f".test-finalize-release-{args.transaction_id}"; marker_preexisting=lexists(marker); marker.write_text("once"); (None if marker_preexisting else (_ for _ in ()).throw(OSError("injected_finalize_after_release")))',
        )],
        "finalize_after_tx": [(
            "shutil.rmtree(tx); fsync_dir(ROOT)",
            'shutil.rmtree(tx); fsync_dir(ROOT); raise OSError("injected_finalize_after_tx")',
        )],
    }
    for old, new in replacements.get(failure or "", []):
        source = source.replace(old, new, 1)
    return source


def _write_lock(remote: Path, transaction_id: str, acquired_at: int) -> Path:
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    transaction_installer = (
        remote
        / f"{bootstrap.TRANSACTION_PREFIX}{transaction_id}"
        / bootstrap.INSTALLER_NAME
    )
    stage_installer = (
        remote / f"{bootstrap.STAGE_PREFIX}{transaction_id}" / bootstrap.INSTALLER_NAME
    )
    installer = (
        transaction_installer if transaction_installer.exists() else stage_installer
    )
    installer_sha256 = (
        hashlib.sha256(installer.read_bytes()).hexdigest()
        if installer.exists()
        else "0" * 64
    )
    lock.write_text(
        json.dumps(
            {
                "schemaVersion": "aurora-deploy-bootstrap-lock-v4",
                "transactionId": transaction_id,
                "acquiredAt": acquired_at,
                "installerSha256": installer_sha256,
                "processId": 2_000_000_000,
                "processStartTicks": 0,
                "bootId": "00000000-0000-4000-8000-000000000000",
                "probeNonce": "a" * 32,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return lock


def _age_lock(lock: Path) -> None:
    record = json.loads(lock.read_text())
    record["acquiredAt"] = int(time.time()) - bootstrap.STALE_LOCK_SECONDS - 1
    lock.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))


def _command(
    remote: Path,
    txid: str,
    payload: bootstrap.Payload,
    mode: str,
    *,
    replace: bool = False,
    failure: str | None = None,
) -> list[str]:
    stage = remote / f"{bootstrap.STAGE_PREFIX}{txid}"
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    installer = (
        stage
        if mode in {"install", "abort-stage"}
        or (mode == "recover-lock" and not tx.exists())
        else tx
    ) / bootstrap.INSTALLER_NAME
    if mode == "install" or not installer.exists():
        installer.write_text(_installer_source(remote, failure=failure))
    command = [sys.executable, str(installer), mode, "--transaction-id", txid]
    if mode == "install":
        command += [
            "--expected-configuration-sha256",
            hashlib.sha256((remote / "configuration.yaml").read_bytes()).hexdigest(),
            "--expected-component-tree-sha256",
            _empty_component_digest(),
            "--expected-trust-sha256",
            "absent",
            "--payload-manifest-sha256",
            payload.manifest_sha256,
            "--expected-installer-sha256",
            hashlib.sha256(installer.read_bytes()).hexdigest(),
        ]
        if replace:
            command.append("--replace-exact")
    else:
        command += [
            "--expected-installer-sha256",
            hashlib.sha256(installer.read_bytes()).hexdigest(),
        ]
        if mode != "abort-stage":
            journal_path = tx / "transaction.json"
            if journal_path.exists():
                journal = json.loads(journal_path.read_text())
                prestate = journal["prestate"]
                configuration_sha256 = prestate["configuration"]["sha256"]
                component_tree_sha256 = bootstrap._component_state_digest(
                    prestate["component"]
                )
                trust_sha256 = prestate["trust"].get("sha256", "absent")
                payload_manifest_sha256 = journal["payloadManifestSha256"]
            else:
                configuration_sha256 = hashlib.sha256(
                    (remote / "configuration.yaml").read_bytes()
                ).hexdigest()
                component_tree_sha256 = _empty_component_digest()
                trust_sha256 = "absent"
                payload_manifest_sha256 = payload.manifest_sha256
            command += [
                "--expected-configuration-sha256",
                configuration_sha256,
                "--expected-component-tree-sha256",
                component_tree_sha256,
                "--expected-trust-sha256",
                trust_sha256,
                "--payload-manifest-sha256",
                payload_manifest_sha256,
            ]
            if journal_path.exists() and journal.get("replaceExact") is True:
                command.append("--replace-exact")
    return command


def _run(
    remote: Path,
    txid: str,
    payload: bootstrap.Payload,
    mode: str,
    *,
    replace: bool = False,
    failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(
            remote,
            txid,
            payload,
            mode,
            replace=replace,
            failure=failure,
        ),
        check=False,
        capture_output=True,
        text=True,
    )


def test_install_uses_global_lock_immediate_cas_and_exact_post_readback(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    result = _run(remote, txid, payload, "install")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "installed"
    expected_tree = hashlib.sha256(
        bootstrap._canonical(
            {
                "exists": True,
                "files": {
                    Path(item.relative_path).name: item.sha256
                    for item in payload.files
                    if item.relative_path.startswith("custom_components/")
                },
                "directories": [],
            }
        )
    ).hexdigest()
    assert receipt["componentTreeSha256"] == expected_tree
    assert (remote / "configuration.yaml").read_text().count("aurora_deploy:") == 1
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()

    remote2, txid2, payload2 = _prepare_remote(tmp_path / "second", root)
    _write_lock(remote2, str(uuid.uuid4()), int(time.time()))
    blocked = _run(remote2, txid2, payload2, "install")
    assert blocked.returncode == 1
    assert "global_lock_held" in blocked.stderr
    assert not (remote2 / "custom_components/aurora_deploy").exists()


def test_configuration_snapshot_prevents_transient_aba_bytes_from_installing(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    installed = _run(remote, txid, payload, "install", failure="config_aba")
    assert installed.returncode == 0, installed.stderr
    configuration = (remote / "configuration.yaml").read_bytes()
    assert configuration == b"default_config:\naurora_deploy:\n"
    assert b"transient-b" not in configuration


def test_extra_component_directory_requires_replace_exact_and_is_preserved(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    component = remote / "custom_components/aurora_deploy"
    component.mkdir()
    for item in payload.files:
        if item.relative_path.startswith("custom_components/"):
            (component / Path(item.relative_path).name).write_bytes(item.content)
    (component / "external-empty-directory").mkdir()
    expected_tree = hashlib.sha256(
        bootstrap._canonical(
            {
                "exists": True,
                "files": {
                    Path(item.relative_path).name: item.sha256
                    for item in payload.files
                    if item.relative_path.startswith("custom_components/")
                },
                "directories": ["external-empty-directory"],
            }
        )
    ).hexdigest()
    command = _command(remote, txid, payload, "install")
    command[command.index("--expected-component-tree-sha256") + 1] = expected_tree
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "replace_exact_required" in rejected.stderr
    assert (component / "external-empty-directory").is_dir()


def test_duplicate_top_level_configuration_key_never_mutates_prestate(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    before = b"aurora_deploy:\ndefault_config:\naurora_deploy:\n"
    (remote / "configuration.yaml").write_bytes(before)
    failed = _run(remote, txid, payload, "install")
    assert failed.returncode == 1
    assert "configuration_key_duplicate" in failed.stderr
    assert (remote / "configuration.yaml").read_bytes() == before
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()


def test_atomic_lock_orphan_recovery_and_concurrent_exclusion(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    orphan_remote, orphan_txid, orphan_payload = _prepare_remote(
        tmp_path / "orphan", root
    )
    orphan = orphan_remote / (f".aurora-deploy-bootstrap-lock-candidate-{orphan_txid}")
    orphan.write_bytes(b"interrupted-before-publish")
    stale = time.time() - bootstrap.STALE_LOCK_SECONDS - 1
    os.utime(orphan, (stale, stale))
    recovered = _run(
        orphan_remote,
        orphan_txid,
        orphan_payload,
        "recover-lock",
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["status"] == "lock_recovered"
    assert not orphan.exists()
    assert not (orphan_remote / ".aurora-deploy-bootstrap-global.lock").exists()

    remote, first_txid, payload = _prepare_remote(tmp_path / "concurrent", root)
    second_txid = str(uuid.uuid4())
    _write_stage(remote, second_txid, payload)
    first_command = _command(
        remote,
        first_txid,
        payload,
        "install",
        failure="lock_hold",
    )
    second_command = _command(
        remote,
        second_txid,
        payload,
        "install",
        failure="lock_hold",
    )
    first = subprocess.Popen(
        first_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    deadline = time.monotonic() + 5
    while not lock.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lock.is_file()
    lock_record = json.loads(lock.read_text())
    assert lock_record["transactionId"] == first_txid

    second = subprocess.run(
        second_command,
        check=False,
        capture_output=True,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)
    assert first.returncode == 0, first_stderr
    assert json.loads(first_stdout)["status"] == "installed"
    assert second.returncode == 1
    assert "global_lock_held" in second.stderr
    assert not lock.exists()
    assert not list(remote.glob(".aurora-deploy-bootstrap-lock-candidate-*"))


def test_lock_link_failure_cleans_candidate_and_is_immediately_retryable(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    failed = _run(remote, txid, payload, "install", failure="lock_link_failure")
    assert failed.returncode == 1
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()
    assert not list(remote.glob(".aurora-deploy-bootstrap-lock-candidate-*"))
    retried = _run(remote, txid, payload, "install")
    assert retried.returncode == 0, retried.stderr


def test_partial_lock_write_cleans_candidate_and_is_immediately_retryable(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    failed = _run(remote, txid, payload, "install", failure="lock_partial_write")
    assert failed.returncode == 1
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()
    assert not list(remote.glob(".aurora-deploy-bootstrap-lock-candidate-*"))
    assert _run(remote, txid, payload, "install").returncode == 0


def test_crashed_partial_runtime_probe_is_authenticated_by_lock_and_recovered(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    interrupted = _run(
        remote, txid, payload, "install", failure="runtime_probe_partial_exit"
    )
    assert interrupted.returncode == 74
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    assert lock.is_file()
    partial = list(remote.glob(f".aurora-deploy-bootstrap-runtime-source-{txid}-*"))
    assert len(partial) == 1
    assert len(partial[0].read_bytes()) == 1
    _age_lock(lock)
    recovered = _run(remote, txid, payload, "recover-lock")
    assert recovered.returncode == 0, recovered.stderr
    assert not lock.exists()
    assert not partial[0].exists()


def test_stale_age_never_recovers_a_still_running_lock_owner(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    lock = _write_lock(
        remote, txid, int(time.time()) - bootstrap.STALE_LOCK_SECONDS - 1
    )
    record = json.loads(lock.read_text())
    record["processId"] = os.getpid()
    if sys.platform == "linux":
        raw = Path(f"/proc/{os.getpid()}/stat").read_text()
        record["processStartTicks"] = int(raw[raw.rfind(")") + 2 :].split()[19])
        record["bootId"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    else:
        record["processStartTicks"] = 0
        record["bootId"] = "00000000-0000-4000-8000-000000000000"
    lock.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
    rejected = _run(remote, txid, payload, "recover-lock")
    assert rejected.returncode == 1
    assert "global_lock_owner_still_running" in rejected.stderr
    assert lock.is_file()


@pytest.mark.parametrize(
    "failure", ("runtime_foreign_destination", "runtime_hardlink_failure")
)
def test_runtime_probe_preserves_foreign_paths_and_cleans_owned_residue(
    tmp_path: Path, failure: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / failure, root)
    failed = _run(remote, txid, payload, "install", failure=failure)
    assert failed.returncode == 1
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()
    assert not list(remote.glob(".aurora-deploy-bootstrap-lock-candidate-*"))
    if failure == "runtime_foreign_destination":
        foreign_paths = list(
            remote.glob(f".aurora-deploy-bootstrap-runtime-destination-{txid}-*")
        )
        assert len(foreign_paths) == 1
        foreign = foreign_paths[0]
        assert foreign.read_bytes() == b"foreign-runtime-writer"
    assert not list(remote.glob(f".aurora-deploy-bootstrap-runtime-source-{txid}-*"))
    assert not list(remote.glob(f".aurora-deploy-bootstrap-runtime-link-*-{txid}-*"))

    aborted = _run(remote, txid, payload, "abort-stage")
    assert aborted.returncode == 0, aborted.stderr
    assert not (remote / f"{bootstrap.STAGE_PREFIX}{txid}").exists()
    assert (remote / "configuration.yaml").read_bytes() == b"default_config:\n"
    assert not (remote / "custom_components/aurora_deploy").exists()


@pytest.mark.parametrize(
    ("failure", "destination"),
    (
        ("post_cas_component", "component"),
        ("post_cas_trust", "trust"),
        ("post_cas_config", "configuration"),
    ),
)
def test_post_cas_external_writer_is_never_overwritten(
    tmp_path: Path, failure: str, destination: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / failure, root)
    failed = _run(remote, txid, payload, "install", failure=failure)
    assert failed.returncode == 1
    assert "partial_failure_rollback_failed" in failed.stderr
    if destination == "component":
        assert (remote / "custom_components/aurora_deploy/writer.py").read_bytes() == (
            b"external-writer"
        )
    elif destination == "trust":
        assert (remote / bootstrap.TRUST_PATH).read_bytes() == b"external-writer"
    else:
        assert (remote / "configuration.yaml").read_bytes() == (
            b"external-writer-config\n"
        )
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    assert json.loads(lock.read_text())["transactionId"] == txid


@pytest.mark.parametrize(
    ("phase", "failure", "expected"),
    (
        ("install", "install_recreate_config", b"external-recreated-config\n"),
        ("rollback", "rollback_recreate_config", b"external-rollback-config\n"),
    ),
)
def test_writer_recreation_after_verified_move_is_not_clobbered(
    tmp_path: Path, phase: str, failure: str, expected: bytes
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / phase, root)
    installed = _run(remote, txid, payload, "install", failure=failure)
    if phase == "install":
        failed = installed
    else:
        assert installed.returncode == 0, installed.stderr
        failed = _run(remote, txid, payload, "rollback")
    assert failed.returncode == 1
    assert "partial_failure_rollback_failed" in failed.stderr
    assert (remote / "configuration.yaml").read_bytes() == expected
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    assert json.loads(lock.read_text())["transactionId"] == txid


def test_partial_failure_restores_prestate_and_stale_lock_recovery_is_explicit(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    before = (remote / "configuration.yaml").read_bytes()
    failed = _run(remote, txid, payload, "install", failure="after_component")
    assert failed.returncode == 1
    assert "install_failed_rolled_back" in failed.stderr
    assert (remote / "configuration.yaml").read_bytes() == before
    assert not (remote / "custom_components/aurora_deploy").exists()

    remote2, txid2, payload2 = _prepare_remote(tmp_path / "recover", root)
    assert _run(remote2, txid2, payload2, "install").returncode == 0
    lock = _write_lock(
        remote2,
        txid2,
        int(time.time()) - bootstrap.STALE_LOCK_SECONDS - 1,
    )
    recovered = _run(remote2, txid2, payload2, "recover-lock")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["status"] == "lock_recovered"
    assert not lock.exists()


@pytest.mark.parametrize(
    "failure",
    (
        "init_exit_after_mkdir",
        "init_exit_after_installer",
        "init_exit_after_replacements",
        "journal_exit_initial",
    ),
)
def test_prepublication_crashes_have_strict_stale_recovery(
    tmp_path: Path, failure: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / failure, root)
    interrupted = _run(remote, txid, payload, "install", failure=failure)
    assert interrupted.returncode in {71, 72}
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    assert lock.is_file()
    initialization = remote / f"{bootstrap.TRANSACTION_INIT_PREFIX}{txid}"
    assert initialization.is_dir()
    assert not (remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}").exists()
    _age_lock(lock)

    recovered = _run(remote, txid, payload, "recover-lock")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["status"] == "lock_recovered"
    assert not lock.exists()
    assert not initialization.exists()
    assert (remote / "configuration.yaml").read_bytes() == b"default_config:\n"
    assert not (remote / "custom_components/aurora_deploy").exists()
    assert not (remote / bootstrap.TRUST_PATH).exists()


def test_later_journal_temp_crash_is_recovered_without_truncation(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    interrupted = _run(remote, txid, payload, "install", failure="journal_exit_later")
    assert interrupted.returncode == 73
    lock = remote / ".aurora-deploy-bootstrap-global.lock"
    _age_lock(lock)
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    assert list(tx.glob("transaction.json.new-*"))

    recovered = _run(remote, txid, payload, "recover-lock")
    assert recovered.returncode == 0, recovered.stderr
    assert not lock.exists()
    assert not list(tx.glob("transaction.json.new-*"))
    journal = json.loads((tx / "transaction.json").read_text())
    assert journal["status"] == "rolled_back_after_recovery"
    assert (remote / "configuration.yaml").read_bytes() == b"default_config:\n"


def test_abort_stage_cleans_released_lock_initialization_residue(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    failed = _run(
        remote,
        txid,
        payload,
        "install",
        failure="init_raise_after_replacements",
    )
    assert failed.returncode == 1
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()
    initialization = remote / f"{bootstrap.TRANSACTION_INIT_PREFIX}{txid}"
    assert initialization.is_dir()

    aborted = _run(remote, txid, payload, "abort-stage")
    assert aborted.returncode == 0, aborted.stderr
    assert json.loads(aborted.stdout)["status"] == "stage_aborted"
    assert not initialization.exists()
    assert not (remote / f"{bootstrap.STAGE_PREFIX}{txid}").exists()
    assert (remote / "configuration.yaml").read_bytes() == b"default_config:\n"


def test_rollback_needs_only_remote_transaction_and_fails_on_missing_prestate(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    component = remote / "custom_components/aurora_deploy"
    component.mkdir()
    (component / "legacy.py").write_bytes(b"legacy")
    trust = remote / bootstrap.TRUST_PATH
    trust.write_bytes(b"legacy trust")
    # Pin this divergent prestate for the installer invocation.
    stage_installer = (
        remote / f"{bootstrap.STAGE_PREFIX}{txid}" / bootstrap.INSTALLER_NAME
    )
    stage_installer.write_text(_installer_source(remote))
    # Invoke directly with digests matching the divergent current state.
    tree = {
        "exists": True,
        "files": {"legacy.py": hashlib.sha256(b"legacy").hexdigest()},
        "directories": [],
    }
    command = [
        sys.executable,
        str(stage_installer),
        "install",
        "--transaction-id",
        txid,
        "--expected-configuration-sha256",
        hashlib.sha256((remote / "configuration.yaml").read_bytes()).hexdigest(),
        "--expected-component-tree-sha256",
        hashlib.sha256(bootstrap._canonical(tree)).hexdigest(),
        "--expected-trust-sha256",
        hashlib.sha256(b"legacy trust").hexdigest(),
        "--payload-manifest-sha256",
        payload.manifest_sha256,
        "--expected-installer-sha256",
        hashlib.sha256(stage_installer.read_bytes()).hexdigest(),
        "--replace-exact",
    ]
    assert subprocess.run(command, check=False).returncode == 0
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    (tx / "previous-trust").unlink()
    shutil.rmtree(remote / f"{bootstrap.STAGE_PREFIX}{txid}")
    current_component = {item.name: item.read_bytes() for item in component.iterdir()}
    rollback = _run(remote, txid, payload, "rollback")
    assert rollback.returncode == 1
    assert "recovery_artifact_missing" in rollback.stderr
    assert {
        item.name: item.read_bytes() for item in component.iterdir()
    } == current_component


def test_recovery_rejects_remote_journal_prestate_tampering(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    journal_path = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}" / "transaction.json"
    journal = json.loads(journal_path.read_text())
    original_configuration = journal["prestate"]["configuration"]["sha256"]
    journal["prestate"]["configuration"]["sha256"] = "f" * 64
    journal_path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
    command = _command(remote, txid, payload, "rollback")
    command[command.index("--expected-configuration-sha256") + 1] = (
        original_configuration
    )
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "recovery_pin_mismatch" in rejected.stderr
    assert (remote / "custom_components/aurora_deploy").is_dir()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("schemaVersion", "wrong-schema"),
        ("transactionId", str(uuid.uuid4())),
        ("status", "unknown"),
        ("payloadManifestSha256", 7),
        ("replaceExact", "false"),
    ),
)
def test_remote_journal_summary_validation_is_strict(
    tmp_path: Path, field: str, replacement: Any
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / field, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    journal = json.loads(
        (
            remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}" / "transaction.json"
        ).read_text()
    )
    journal[field] = replacement
    with pytest.raises(bootstrap.BootstrapError, match="transaction_invalid"):
        bootstrap._validated_transaction_journal(journal, txid)


@pytest.mark.parametrize(
    "status",
    (
        "installed",
        "restart_verified",
        "finalize_prepared",
        "rollback_prepared",
        "rolled_back",
        "rolled_back_after_failure",
        "rolled_back_after_recovery",
        "rollback_finalize_prepared",
    ),
)
def test_journal_summary_hashes_are_status_dependent(
    tmp_path: Path, status: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / status, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    journal = json.loads(
        (
            remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}" / "transaction.json"
        ).read_text()
    )
    journal["status"] = status
    source = (
        journal["installed"]
        if status
        in {"installed", "restart_verified", "finalize_prepared", "rollback_prepared"}
        else journal["prestate"]
    )
    journal["configurationSha256"] = source["configuration"]["sha256"]
    journal["componentTreeSha256"] = bootstrap._component_state_digest(
        source["component"]
    )
    journal["trustSha256"] = source["trust"].get("sha256", "absent")
    bootstrap._validated_transaction_journal(journal, txid)
    journal["configurationSha256"] = "f" * 64
    with pytest.raises(bootstrap.BootstrapError, match="transaction_invalid"):
        bootstrap._validated_transaction_journal(journal, txid)

    journal["status"] = "prepared"
    with pytest.raises(bootstrap.BootstrapError, match="transaction_invalid"):
        bootstrap._validated_transaction_journal(journal, txid)


@pytest.mark.parametrize(
    "field",
    (
        "installerSha256",
        "payloadManifestSha256",
        "prestateConfigurationSha256",
        "prestateComponentTreeSha256",
        "prestateTrustSha256",
        "replaceExact",
    ),
)
def test_remote_status_must_match_every_independent_local_pin(
    field: str,
) -> None:
    txid = str(uuid.uuid4())
    local = _local_record_value(txid)
    bootstrap._write_local_record(txid, local, create=True)
    status: dict[str, Any] = {
        "status": "installed",
        "transactionId": txid,
        "transactionPresent": True,
        "stagePresent": True,
        "lockCandidatePresent": False,
        "initializationPresent": False,
        "installerSha256": local["installerSha256"],
        "payloadManifestSha256": local["payloadManifestSha256"],
        "prestateConfigurationSha256": local["expectedConfigurationSha256"],
        "prestateComponentTreeSha256": local["expectedComponentTreeSha256"],
        "prestateTrustSha256": local["expectedTrustSha256"],
        "replaceExact": False,
    }
    bootstrap._bind_status_to_local(status, txid, required=True)
    status[field] = True if field == "replaceExact" else "f" * 64
    with pytest.raises(bootstrap.BootstrapError, match="remote_local_pin_mismatch"):
        bootstrap._bind_status_to_local(status, txid, required=True)


@pytest.mark.parametrize("destination", ("component", "trust", "configuration"))
def test_rollback_rejects_and_preserves_third_state_destination_drift(
    tmp_path: Path, destination: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / destination, root)
    installed = _run(remote, txid, payload, "install")
    assert installed.returncode == 0, installed.stderr
    if destination == "component":
        drift_path = remote / "custom_components/aurora_deploy/adapter.py"
    elif destination == "trust":
        drift_path = remote / bootstrap.TRUST_PATH
    else:
        drift_path = remote / "configuration.yaml"
    drift = f"external-{destination}-drift\n".encode()
    drift_path.write_bytes(drift)
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    artifacts_before = {
        item.relative_to(tx).as_posix(): item.read_bytes()
        for item in tx.rglob("*")
        if item.is_file()
    }

    rolled_back = _run(remote, txid, payload, "rollback")
    assert rolled_back.returncode == 1
    assert "rollback_destination_drift" in rolled_back.stderr
    assert drift_path.read_bytes() == drift
    assert {
        item.relative_to(tx).as_posix(): item.read_bytes()
        for item in tx.rglob("*")
        if item.is_file()
    } == artifacts_before
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()


def test_successful_rollback_can_finalize_all_recovery_artifacts(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    rolled_back = _run(remote, txid, payload, "rollback")
    assert rolled_back.returncode == 0, rolled_back.stderr
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    assert (tx / "rollback-displaced-configuration").is_file()

    finalized = _run(remote, txid, payload, "finalize")
    assert finalized.returncode == 0, finalized.stderr
    assert not tx.exists()
    assert not (remote / f"{bootstrap.STAGE_PREFIX}{txid}").exists()
    assert (remote / "configuration.yaml").read_bytes() == b"default_config:\n"


@pytest.mark.parametrize("terminal_mode", ("rollback", "finalize"))
def test_generated_pycache_does_not_strand_recovery_or_finalize(
    tmp_path: Path, terminal_mode: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / terminal_mode, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    pycache = remote / "custom_components/aurora_deploy/__pycache__"
    pycache.mkdir()
    (pycache / "adapter.cpython-313.pyc").write_bytes(b"generated-bytecode")
    if terminal_mode == "rollback":
        result = _run(remote, txid, payload, "rollback")
        assert result.returncode == 0, result.stderr
        assert not (remote / "custom_components/aurora_deploy").exists()
    else:
        assert _run(remote, txid, payload, "mark-verified").returncode == 0
        tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
        journal_path = tx / "transaction.json"
        journal = json.loads(journal_path.read_text())
        journal["rollbackDeadline"] = int(time.time()) - 1
        journal_path.write_text(
            json.dumps(journal, sort_keys=True, separators=(",", ":"))
        )
        result = _run(remote, txid, payload, "finalize")
        assert result.returncode == 0, result.stderr
        assert not tx.exists()


def test_finalize_requires_restart_verified_readback_and_expired_rollback_window(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path, root)
    assert _run(remote, txid, payload, "install").returncode == 0
    marked = _run(remote, txid, payload, "mark-verified")
    assert marked.returncode == 0, marked.stderr
    early = _run(remote, txid, payload, "finalize")
    assert early.returncode == 1
    assert "finalize_not_allowed" in early.stderr
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    journal_path = tx / "transaction.json"
    journal = json.loads(journal_path.read_text())
    journal["rollbackDeadline"] = int(time.time()) - 1
    journal_path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
    finalized = _run(remote, txid, payload, "finalize")
    assert finalized.returncode == 0, finalized.stderr
    assert not tx.exists()
    assert not (remote / f"{bootstrap.STAGE_PREFIX}{txid}").exists()


@pytest.mark.parametrize(
    "failure",
    (
        "finalize_after_prepared",
        "finalize_after_stage",
        "finalize_after_release",
        "finalize_after_tx",
    ),
)
def test_finalize_crash_boundaries_never_leave_unrecoverable_lock(
    tmp_path: Path, failure: str
) -> None:
    root = _source_root(tmp_path)
    remote, txid, payload = _prepare_remote(tmp_path / failure, root)
    installed = _run(remote, txid, payload, "install", failure=failure)
    assert installed.returncode == 0, installed.stderr
    marked = _run(remote, txid, payload, "mark-verified")
    assert marked.returncode == 0, marked.stderr
    tx = remote / f"{bootstrap.TRANSACTION_PREFIX}{txid}"
    stage = remote / f"{bootstrap.STAGE_PREFIX}{txid}"
    journal_path = tx / "transaction.json"
    journal = json.loads(journal_path.read_text())
    journal["rollbackDeadline"] = int(time.time()) - 1
    journal_path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))

    interrupted = _run(remote, txid, payload, "finalize")
    assert interrupted.returncode == 1
    assert not (remote / ".aurora-deploy-bootstrap-global.lock").exists()
    if failure == "finalize_after_tx":
        assert not tx.exists()
        assert not stage.exists()
    else:
        assert (tx / bootstrap.INSTALLER_NAME).is_file()
        retried = _run(remote, txid, payload, "finalize")
        assert retried.returncode == 0, retried.stderr
        assert json.loads(retried.stdout)["status"] == "finalized"
        assert not tx.exists()
        assert not stage.exists()


@pytest.mark.asyncio
class _FinalizeDeathIngress:
    def __init__(
        self,
        local: dict[str, Any],
        configuration: bytes,
        component_sha: str,
        trust_sha: str,
    ) -> None:
        self.local = local
        self.configuration = configuration
        self.component_sha = component_sha
        self.trust_sha = trust_sha
        self.finalized = False
        self.execute_modes: list[str] = []

    def readback(self, transaction_id: str) -> dict[str, Any]:
        if self.finalized:
            return {
                "status": "not_found", "transactionId": transaction_id,
                "stagePresent": False, "lockCandidatePresent": False,
                "transactionPresent": False, "initializationPresent": False,
                "lockHeld": False, "lockOwnerMatches": False, "verified": False,
            }
        local = self.local
        return {
            "status": "installed", "transactionId": transaction_id,
            "payloadManifestSha256": local["payloadManifestSha256"],
            "installerSha256": local["installerSha256"], "replaceExact": False,
            "prestateConfigurationSha256": local["expectedConfigurationSha256"],
            "prestateComponentTreeSha256": local["expectedComponentTreeSha256"],
            "prestateTrustSha256": "absent",
            "configurationSha256": hashlib.sha256(self.configuration).hexdigest(),
            "componentTreeSha256": self.component_sha, "trustSha256": self.trust_sha,
            "rollbackDeadline": int(time.time()) - 1, "stagePresent": True,
            "lockCandidatePresent": False, "transactionPresent": True,
            "initializationPresent": False, "lockHeld": False,
            "lockOwnerMatches": False, "verified": True,
        }

    def configuration_bytes(self) -> bytes:
        return self.configuration

    def component_tree_sha256(self) -> str:
        return self.component_sha

    def trust_sha256(self) -> str:
        return self.trust_sha

    def execute(self, *, mode: str, transaction_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.execute_modes.append(mode)
        if mode == "mark-verified":
            return {"status": "restart_verified", "transactionId": transaction_id}
        self.finalized = True
        raise RuntimeError("simulated local process death after remote finalize")


async def test_finalize_authorization_reconciles_process_death_after_remote_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    txid = str(uuid.uuid4())
    local = _local_record_value(txid)
    bootstrap._write_local_record(txid, local, create=True)
    configuration = b"verified-config\n"
    component_sha = "4" * 64
    trust_sha = "5" * 64

    async def open_editor(*_args: Any) -> tuple[str, str, str]:
        return "started", "/api/hassio_ingress/fixed", "A" * 32

    monkeypatch.setattr(bootstrap, "_open_editor", open_editor)

    ingress = _FinalizeDeathIngress(local, configuration, component_sha, trust_sha)

    async def run() -> dict[str, Any]:
        return await bootstrap.bootstrap(
            mode="finalize",
            source_root=tmp_path / "removed-worktree",
            transaction_id=txid,
            ingress_client_factory=lambda *_args: ingress,
            config_check=lambda *_args: None,
            component_route_check=lambda *_args: None,
            credential_loader=_test_credentials,
        )

    with pytest.raises(bootstrap.BootstrapError, match="operation_failed"):
        await run()
    assert bootstrap._read_local_record(txid)["status"] == "finalize_authorized"
    result = await run()
    assert result == {
        "status": "finalized",
        "transactionId": txid,
        "payloadManifestSha256": local["payloadManifestSha256"],
        "reconciled": True,
    }
    assert await run() == result
    assert ingress.execute_modes == ["mark-verified", "finalize"]


def test_lost_exec_response_reconciles_from_remote_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(bootstrap.FileEditorIngressClient)
    monkeypatch.setattr(
        client,
        "_stage",
        lambda _txid, _relative="": "/homeassistant/stage/installer.py",
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bootstrap.BootstrapError("file_editor_transport_failure")
        ),
    )
    monkeypatch.setattr(
        client, "_download_path", lambda _path: bootstrap.INSTALLER_SOURCE
    )
    monkeypatch.setattr(client, "lock_candidate_exists", lambda _txid: False)
    monkeypatch.setattr(
        client,
        "readback",
        lambda txid: {
            "status": "installed",
            "transactionId": txid,
            "payloadManifestSha256": "2" * 64,
            "installerSha256": bootstrap.INSTALLER_SHA256,
            "replaceExact": False,
            "prestateConfigurationSha256": "0" * 64,
            "prestateComponentTreeSha256": "1" * 64,
            "prestateTrustSha256": "absent",
            "stagePresent": True,
            "lockCandidatePresent": False,
            "transactionPresent": True,
            "initializationPresent": False,
            "lockHeld": False,
            "lockOwnerMatches": False,
            "verified": True,
        },
    )
    result = client.execute(
        mode="install",
        transaction_id=str(uuid.uuid4()),
        arguments={
            "expected-configuration-sha256": "0" * 64,
            "expected-component-tree-sha256": "1" * 64,
            "expected-trust-sha256": "absent",
            "payload-manifest-sha256": "2" * 64,
            "expected-installer-sha256": bootstrap.INSTALLER_SHA256,
        },
    )
    assert result["reconciled"] is True


def test_lost_finalize_response_requires_transaction_and_lock_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(bootstrap.FileEditorIngressClient)
    monkeypatch.setattr(
        client,
        "_transaction",
        lambda _txid, _relative="": "/homeassistant/transaction/installer.py",
    )
    monkeypatch.setattr(
        client,
        "_download_path",
        lambda _path: bootstrap.INSTALLER_SOURCE,
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bootstrap.BootstrapError("file_editor_transport_failure")
        ),
    )
    monkeypatch.setattr(client, "lock_candidate_exists", lambda _txid: False)
    monkeypatch.setattr(client, "transaction_exists", lambda _txid: False)
    monkeypatch.setattr(client, "stage_exists", lambda _txid: False)
    txid = str(uuid.uuid4())
    monkeypatch.setattr(
        client,
        "readback",
        lambda _txid: {
            "status": "not_found",
            "transactionId": txid,
            "stagePresent": False,
            "lockCandidatePresent": False,
            "transactionPresent": False,
            "initializationPresent": False,
            "lockHeld": True,
            "lockOwnerMatches": False,
            "verified": False,
        },
    )
    with pytest.raises(bootstrap.BootstrapError, match="installer_result_ambiguous"):
        client.execute(
            mode="finalize",
            transaction_id=txid,
            arguments={"expected-installer-sha256": bootstrap.INSTALLER_SHA256},
        )

    monkeypatch.setattr(
        client,
        "readback",
        lambda _txid: {
            "status": "not_found",
            "transactionId": txid,
            "stagePresent": False,
            "lockCandidatePresent": False,
            "transactionPresent": False,
            "initializationPresent": False,
            "lockHeld": False,
            "lockOwnerMatches": False,
            "verified": False,
        },
    )
    assert client.execute(
        mode="finalize",
        transaction_id=txid,
        arguments={"expected-installer-sha256": bootstrap.INSTALLER_SHA256},
    ) == {
        "status": "finalized",
        "transactionId": txid,
        "reconciled": True,
    }


@pytest.mark.parametrize(
    ("mode", "terminal", "wrapper_kind"),
    (
        ("install", "installed", "nonzero"),
        ("rollback", "rolled_back", "nonzero"),
        ("recover-lock", "installed", "nonzero"),
        ("finalize", "not_found", "nonzero"),
        ("install", "installed", "malformed"),
        ("install", "installed", "stderr"),
        ("install", "installed", "surrogate"),
    ),
)
def test_every_post_exec_unknown_outcome_uses_exact_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    terminal: str,
    wrapper_kind: str,
) -> None:
    txid = str(uuid.uuid4())
    client = object.__new__(bootstrap.FileEditorIngressClient)
    monkeypatch.setattr(
        client,
        "_stage",
        lambda _txid, _relative="": "/homeassistant/stage/installer.py",
    )
    monkeypatch.setattr(
        client,
        "_transaction",
        lambda _txid, _relative="": "/homeassistant/transaction/installer.py",
    )
    monkeypatch.setattr(client, "transaction_exists", lambda _txid: True)
    monkeypatch.setattr(
        client, "_download_path", lambda _path: bootstrap.INSTALLER_SOURCE
    )
    wrappers = {
        "nonzero": {"error": False, "returncode": 1, "stdout": "", "stderr": ""},
        "malformed": {
            "error": False,
            "returncode": 0,
            "stdout": "{",
            "stderr": "",
        },
        "stderr": {
            "error": False,
            "returncode": 0,
            "stdout": "{}",
            "stderr": "late-wrapper-error",
        },
        "surrogate": {
            "error": False,
            "returncode": 0,
            "stdout": "\ud800",
            "stderr": "",
        },
    }
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **kwargs: {
            **wrappers[wrapper_kind],
            "message": f"Command executed: {kwargs['form']['command']}",
        },
    )
    transaction_present = terminal != "not_found"
    status: dict[str, Any] = {
        "status": terminal,
        "transactionId": txid,
        "stagePresent": transaction_present,
        "lockCandidatePresent": False,
        "transactionPresent": transaction_present,
        "initializationPresent": False,
        "lockHeld": False,
        "lockOwnerMatches": False,
        "verified": transaction_present,
    }
    if transaction_present:
        status.update(
            {
                "installerSha256": bootstrap.INSTALLER_SHA256,
                "payloadManifestSha256": "2" * 64,
                "replaceExact": False,
                "prestateConfigurationSha256": "0" * 64,
                "prestateComponentTreeSha256": "1" * 64,
                "prestateTrustSha256": "absent",
            }
        )
    monkeypatch.setattr(client, "readback", lambda _txid: dict(status))
    result = client.execute(
        mode=mode,
        transaction_id=txid,
        arguments={
            "expected-configuration-sha256": "0" * 64,
            "expected-component-tree-sha256": "1" * 64,
            "expected-trust-sha256": "absent",
            "payload-manifest-sha256": "2" * 64,
            "expected-installer-sha256": bootstrap.INSTALLER_SHA256,
        },
    )
    assert result["reconciled"] is True
    assert (
        result["status"]
        == {
            "install": "installed",
            "rollback": "rolled_back",
            "recover-lock": "lock_recovered",
            "finalize": "finalized",
        }[mode]
    )


@pytest.mark.parametrize(
    ("mode", "terminal"),
    (("rollback", "rolled_back"), ("recover-lock", "installed")),
)
def test_lost_recovery_response_preserves_true_replace_exact_pin(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    terminal: str,
) -> None:
    txid = str(uuid.uuid4())
    local = _local_record_value(txid, replace_exact=True)
    bootstrap._write_local_record(txid, local, create=True)
    arguments = bootstrap._persist_local_request(txid, mode)
    assert arguments["replace-exact"] is True

    client = object.__new__(bootstrap.FileEditorIngressClient)
    monkeypatch.setattr(
        client,
        "_transaction",
        lambda _txid, _relative="": "/homeassistant/transaction/installer.py",
    )
    monkeypatch.setattr(client, "transaction_exists", lambda _txid: True)
    monkeypatch.setattr(
        client, "_download_path", lambda _path: bootstrap.INSTALLER_SOURCE
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bootstrap.BootstrapError("file_editor_transport_failure")
        ),
    )
    status = {
        "status": terminal,
        "transactionId": txid,
        "installerSha256": local["installerSha256"],
        "payloadManifestSha256": local["payloadManifestSha256"],
        "replaceExact": True,
        "prestateConfigurationSha256": local["expectedConfigurationSha256"],
        "prestateComponentTreeSha256": local["expectedComponentTreeSha256"],
        "prestateTrustSha256": local["expectedTrustSha256"],
        "stagePresent": True,
        "lockCandidatePresent": False,
        "transactionPresent": True,
        "initializationPresent": False,
        "lockHeld": False,
        "lockOwnerMatches": False,
        "verified": True,
    }
    monkeypatch.setattr(client, "readback", lambda _txid: dict(status))
    result = client.execute(mode=mode, transaction_id=txid, arguments=arguments)
    assert result["reconciled"] is True
    assert result["status"] == (
        "rolled_back" if mode == "rollback" else "lock_recovered"
    )


@pytest.mark.parametrize(
    ("mismatch", "lock_owner"),
    (("manifest", False), ("replace", False), ("lock", False), ("lock", True)),
)
def test_lost_response_never_accepts_request_collision_or_live_lock(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    lock_owner: bool,
) -> None:
    txid = str(uuid.uuid4())
    client = object.__new__(bootstrap.FileEditorIngressClient)
    monkeypatch.setattr(client, "_stage", lambda *_args: "/fixed/installer.py")
    monkeypatch.setattr(
        client, "_download_path", lambda _path: bootstrap.INSTALLER_SOURCE
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **kwargs: {
            "error": False,
            "message": f"Command executed: {kwargs['form']['command']}",
            "returncode": 1,
            "stdout": "",
            "stderr": "",
        },
    )
    status = {
        "status": "installed",
        "transactionId": txid,
        "installerSha256": bootstrap.INSTALLER_SHA256,
        "payloadManifestSha256": "f" * 64 if mismatch == "manifest" else "2" * 64,
        "replaceExact": mismatch == "replace",
        "prestateConfigurationSha256": "0" * 64,
        "prestateComponentTreeSha256": "1" * 64,
        "prestateTrustSha256": "absent",
        "stagePresent": True,
        "lockCandidatePresent": False,
        "transactionPresent": True,
        "initializationPresent": False,
        "lockHeld": mismatch == "lock",
        "lockOwnerMatches": lock_owner,
        "verified": True,
    }
    monkeypatch.setattr(client, "readback", lambda _txid: dict(status))
    with pytest.raises(bootstrap.BootstrapError, match="installer_result_ambiguous"):
        client.execute(
            mode="install",
            transaction_id=txid,
            arguments={
                "expected-configuration-sha256": "0" * 64,
                "expected-component-tree-sha256": "1" * 64,
                "expected-trust-sha256": "absent",
                "payload-manifest-sha256": "2" * 64,
                "expected-installer-sha256": bootstrap.INSTALLER_SHA256,
            },
        )


@pytest.mark.parametrize(
    ("status_name", "stage", "candidate", "initialization"),
    (
        ("not_found", False, False, False),
        ("staged_partial", True, False, False),
        ("lock_candidate", False, True, False),
        ("initializing", False, False, True),
    ),
)
def test_status_exposes_and_locally_binds_exact_partial_topology(
    monkeypatch: pytest.MonkeyPatch,
    status_name: str,
    stage: bool,
    candidate: bool,
    initialization: bool,
) -> None:
    txid = str(uuid.uuid4())
    client = object.__new__(bootstrap.FileEditorIngressClient)
    monkeypatch.setattr(client, "transaction_exists", lambda _txid: False)
    monkeypatch.setattr(client, "stage_exists", lambda _txid: stage)
    monkeypatch.setattr(client, "lock_candidate_exists", lambda _txid: candidate)
    monkeypatch.setattr(client, "initialization_exists", lambda _txid: initialization)
    monkeypatch.setattr(client, "lock_status", lambda: None)
    status = client.status(txid)
    assert status == {
        "status": status_name,
        "transactionId": txid,
        "stagePresent": stage,
        "lockCandidatePresent": candidate,
        "transactionPresent": False,
        "initializationPresent": initialization,
        "lockHeld": False,
        "lockOwnerMatches": False,
    }
    bootstrap._write_local_record(txid, _local_record_value(txid), create=True)
    assert bootstrap._bind_status_to_local(status, txid, required=True) == status

    contradictory = {**status, "stagePresent": not stage}
    with pytest.raises(bootstrap.BootstrapError, match="remote_local_pin_mismatch"):
        bootstrap._bind_status_to_local(contradictory, txid, required=True)


@pytest.mark.asyncio
class _StatusSocket:
    state = "stopped"

    def __init__(self, *_args: Any) -> None:
        pass

    async def __aenter__(self) -> _StatusSocket:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def call(self, command: str, **fields: Any) -> dict[str, Any]:
        values = _preflight_values()
        if command == "auth/current_user":
            return values["user"]
        if command == "backup/info":
            return values["backup"]
        endpoint = fields["endpoint"]
        if endpoint.endswith("/start"):
            type(self).state = "started"
        if endpoint.endswith("/stop"):
            type(self).state = "stopped"
        data = {
            "/supervisor/info": values["supervisor"],
            "/host/info": values["host"],
            f"/store/addons/{bootstrap.FILE_EDITOR_SLUG}": values["store"],
            f"/addons/{bootstrap.FILE_EDITOR_SLUG}/info": {
                **values["addon"], "state": type(self).state
            },
            f"/addons/{bootstrap.FILE_EDITOR_SLUG}/start": {},
            f"/addons/{bootstrap.FILE_EDITOR_SLUG}/stop": {},
            "/ingress/session": {"session": "A" * 32},
        }.get(endpoint)
        if data is None:
            raise AssertionError(endpoint)
        return {"result": "ok", "data": data}


class _NotFoundIngress:
    def __init__(self, *_args: Any) -> None:
        pass

    def status(self, transaction_id: str) -> dict[str, Any]:
        return {"status": "not_found", "transactionId": transaction_id, "lockHeld": False}


async def test_read_only_status_needs_no_payload_or_stage_and_restores_addon_state(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    txid = str(uuid.uuid4())

    _StatusSocket.state = "stopped"
    result = await bootstrap.bootstrap(
        mode="status",
        source_root=root,
        transaction_id=txid,
        socket_factory=_StatusSocket,
        ingress_client_factory=_NotFoundIngress,
        credential_loader=_test_credentials,
    )
    assert result == {
        "status": "not_found",
        "transactionId": txid,
        "lockHeld": False,
    }
    assert _StatusSocket.state == "stopped"


@pytest.mark.asyncio
async def test_recover_lock_verifiably_cycles_file_editor_and_repairs_crash_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = "started"
    actions: list[str] = []

    async def snapshot(*_args: Any) -> dict[str, Any]:
        values = _preflight_values()
        for key in (
            "backup_id",
            "expected_backup_agent_id",
            "require_backup",
            "now",
        ):
            values.pop(key)
        values["addon"]["state"] = state
        return values

    async def action(
        _url: str, _token: str, requested: str, _socket_factory: Any
    ) -> None:
        nonlocal state
        actions.append(requested)
        state = "started" if requested == "start" else "stopped"

    class Socket:
        def __init__(self, *_args: Any) -> None:
            pass

        async def __aenter__(self) -> Socket:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def call(self, command: str, **_fields: Any) -> dict[str, Any]:
            assert command == "supervisor/api"
            return {"session": "A" * 32}

    monkeypatch.setattr(bootstrap, "_supervisor_snapshot", snapshot)
    monkeypatch.setattr(bootstrap, "_addon_action", action)
    txid = str(uuid.uuid4())
    initial, _ingress, _session = await bootstrap._open_editor(
        "https://ha.example.test",
        "token",
        None,
        None,
        False,
        "recover-lock",
        txid,
        Socket,
    )
    assert initial == "started"
    assert actions == ["stop", "start"]
    assert state == "started"
    assert bootstrap._read_lifecycle_lease() is None

    # Simulate a hard kill after recording an originally-started recovery
    # cycle but before the restart. The next invocation restores it first.
    bootstrap._write_lifecycle_lease(
        "recover-lock", txid, create=True, initial_state="started"
    )
    state = "stopped"
    actions.clear()
    initial, _ingress, _session = await bootstrap._open_editor(
        "https://ha.example.test",
        "token",
        None,
        None,
        False,
        "status",
        txid,
        Socket,
    )
    assert initial == "started"
    assert actions == ["start"]
    assert bootstrap._read_lifecycle_lease() is None

    bootstrap._write_lifecycle_lease("status", txid, create=True)
    lease = bootstrap._read_local_record(bootstrap.LIFECYCLE_RECORD_ID)
    lease["processId"] = 1
    bootstrap._write_local_record(bootstrap.LIFECYCLE_RECORD_ID, lease, create=False)
    actions.clear()
    with pytest.raises(bootstrap.BootstrapError, match="lifecycle_lease_active"):
        await bootstrap._open_editor(
            "https://ha.example.test",
            "token",
            None,
            None,
            False,
            "status",
            txid,
            Socket,
        )
    assert actions == []


@pytest.mark.asyncio
async def test_transaction_is_persisted_before_any_remote_mutation(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path)
    pins = _pins(root)
    txid = str(uuid.uuid4())

    class FailingSocket:
        def __init__(self, *_args: Any) -> None:
            raise AssertionError("factory must only run after local persistence")

    with pytest.raises(bootstrap.BootstrapError, match="operation_failed"):
        await bootstrap.bootstrap(
            mode="install",
            source_root=root,
            backup_id="backup-1",
            transaction_id=txid,
            expected_configuration_sha256="0" * 64,
            expected_component_tree_sha256="1" * 64,
            expected_trust_sha256="absent",
            expected_payload_manifest_sha256=pins["expected_manifest_sha256"],
            expected_release_key_sha256=pins["expected_release_key_sha256"],
            expected_validation_key_sha256=pins["expected_validation_key_sha256"],
            expected_source_revision=pins["expected_source_revision"],
            socket_factory=FailingSocket,
            config_check=lambda *_args: None,
            credential_loader=_test_credentials,
        )
    record = bootstrap.APPROVED_STATE_DIRECTORY / f"{txid}.json"
    assert json.loads(record.read_text())["status"] == "prepared"


@pytest.mark.asyncio
class _PartialUploadIngress:
    def __init__(
        self,
        payload: bootstrap.Payload,
        before: bytes,
        after: bytes,
        installed_tree: str,
        installed_trust: str,
    ) -> None:
        self.payload = payload
        self.before = before
        self.after = after
        self.installed_tree = installed_tree
        self.installed_trust = installed_trust
        self.staged: dict[str, bytes] = {}
        self.upload_counts: dict[str, int] = {}
        self.failed = False
        self.installed = False

    def configuration_bytes(self) -> bytes:
        return self.after if self.installed else self.before

    def component_tree_sha256(self) -> str:
        return self.installed_tree if self.installed else _empty_component_digest()

    def trust_sha256(self) -> str:
        return self.installed_trust if self.installed else "absent"

    def create_stage(self, _txid: str) -> None:
        return None

    def verify_fixed_root_write_capability(self, _txid: str) -> None:
        return None

    def stage_file_exists(self, _txid: str, relative: str) -> bool:
        return relative in self.staged

    def download_stage(self, _txid: str, relative: str) -> bytes:
        return self.staged[relative]

    def upload(self, _txid: str, relative: str, content: bytes) -> None:
        if len(self.staged) == 2 and not self.failed:
            self.failed = True
            raise bootstrap.BootstrapError("remote_upload_failed")
        self.upload_counts[relative] = self.upload_counts.get(relative, 0) + 1
        self.staged[relative] = content

    def execute(self, *, mode: str, transaction_id: str, **_kwargs: Any) -> dict[str, Any]:
        assert mode == "install"
        self.installed = True
        return {
            "status": "installed", "transactionId": transaction_id,
            "payloadManifestSha256": self.payload.manifest_sha256,
            "configurationSha256": hashlib.sha256(self.after).hexdigest(),
            "componentTreeSha256": self.installed_tree,
            "trustSha256": self.installed_trust,
        }


async def test_partial_upload_resumes_same_exact_local_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_root(tmp_path)
    pins = _pins(root)
    payload = _payload(root)
    txid = str(uuid.uuid4())
    before = b"default_config:\n"
    after = before + b"aurora_deploy:\n"
    installed_tree = hashlib.sha256(
        bootstrap._canonical(
            {
                "exists": True,
                "files": {
                    Path(item.relative_path).name: item.sha256
                    for item in payload.files
                    if item.relative_path.startswith("custom_components/")
                },
                "directories": [],
            }
        )
    ).hexdigest()
    installed_trust = next(
        item.sha256
        for item in payload.files
        if item.relative_path == bootstrap.TRUST_PATH
    )

    async def open_editor(*_args: Any) -> tuple[str, str, str]:
        return "started", "/api/hassio_ingress/fixed", "A" * 32

    monkeypatch.setattr(bootstrap, "_open_editor", open_editor)

    ingress = _PartialUploadIngress(
        payload, before, after, installed_tree, installed_trust
    )

    async def run(*, replace_exact: bool = False) -> dict[str, Any]:
        return await bootstrap.bootstrap(
            mode="install",
            source_root=root,
            backup_id="backup-1",
            transaction_id=txid,
            expected_configuration_sha256=hashlib.sha256(before).hexdigest(),
            expected_component_tree_sha256=_empty_component_digest(),
            expected_trust_sha256="absent",
            expected_payload_manifest_sha256=pins["expected_manifest_sha256"],
            expected_release_key_sha256=pins["expected_release_key_sha256"],
            expected_validation_key_sha256=pins["expected_validation_key_sha256"],
            expected_source_revision=pins["expected_source_revision"],
            replace_exact=replace_exact,
            ingress_client_factory=lambda *_args: ingress,
            config_check=lambda *_args: None,
            credential_loader=_test_credentials,
        )

    with pytest.raises(bootstrap.BootstrapError, match="remote_upload_failed"):
        await run()
    first_exact = dict(ingress.staged)
    with pytest.raises(bootstrap.BootstrapError, match="local_transaction_collision"):
        await run(replace_exact=True)
    result = await run()
    assert result["status"] == "installed"
    assert all(ingress.upload_counts[name] == 1 for name in first_exact)
    assert set(ingress.staged) == {
        bootstrap.INSTALLER_NAME,
        *bootstrap.PAYLOAD_PATHS,
        bootstrap.PAYLOAD_MANIFEST_NAME,
    }


@pytest.mark.asyncio
async def test_abort_uses_persisted_old_installer_when_source_checkout_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    txid = str(uuid.uuid4())
    old_installer = bootstrap.INSTALLER_SOURCE
    local = _local_record_value(txid, installer=old_installer)
    bootstrap._write_local_record(txid, local, create=True)
    removed_source = tmp_path / "removed-source-worktree"
    monkeypatch.setattr(bootstrap, "INSTALLER_SOURCE", b"new-current-installer")
    monkeypatch.setattr(
        bootstrap,
        "INSTALLER_SHA256",
        hashlib.sha256(b"new-current-installer").hexdigest(),
    )

    async def open_editor(*_args: Any) -> tuple[str, str, str]:
        return "started", "/api/hassio_ingress/fixed", "A" * 32

    monkeypatch.setattr(bootstrap, "_open_editor", open_editor)

    class Ingress:
        def __init__(self, *_args: Any) -> None:
            self.files: dict[str, bytes] = {}

        def transaction_exists(self, _txid: str) -> bool:
            return False

        def create_stage(self, _txid: str) -> None:
            return None

        def verify_fixed_root_write_capability(self, _txid: str) -> None:
            return None

        def stage_file_exists(self, _txid: str, relative: str) -> bool:
            return relative in self.files

        def download_stage(self, _txid: str, relative: str) -> bytes:
            return self.files[relative]

        def upload(self, _txid: str, relative: str, content: bytes) -> None:
            self.files[relative] = content

        def execute(
            self,
            *,
            mode: str,
            transaction_id: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            assert mode == "abort-stage"
            assert arguments["expected-installer-sha256"] == local["installerSha256"]
            assert self.files[bootstrap.INSTALLER_NAME] == old_installer
            return {"status": "stage_aborted", "transactionId": transaction_id}

    ingress = Ingress()
    result = await bootstrap.bootstrap(
        mode="abort-stage",
        source_root=removed_source,
        transaction_id=txid,
        ingress_client_factory=lambda *_args: ingress,
        credential_loader=_test_credentials,
    )
    assert result["status"] == "stage_aborted"
    assert ingress.files[bootstrap.INSTALLER_NAME] == old_installer


@pytest.mark.asyncio
async def test_invalid_prestate_config_check_has_zero_remote_or_local_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_root(tmp_path)
    pins = _pins(root)

    async def forbidden_open(*_args: Any) -> tuple[str, str, str]:
        raise AssertionError("File Editor must not be opened")

    monkeypatch.setattr(bootstrap, "_open_editor", forbidden_open)
    with pytest.raises(bootstrap.BootstrapError, match="configuration_check_failed"):
        await bootstrap.bootstrap(
            mode="install",
            source_root=root,
            backup_id="backup-1",
            transaction_id=str(uuid.uuid4()),
            expected_configuration_sha256="0" * 64,
            expected_component_tree_sha256="1" * 64,
            expected_trust_sha256="absent",
            expected_payload_manifest_sha256=pins["expected_manifest_sha256"],
            expected_release_key_sha256=pins["expected_release_key_sha256"],
            expected_validation_key_sha256=pins["expected_validation_key_sha256"],
            expected_source_revision=pins["expected_source_revision"],
            config_check=lambda *_args: (_ for _ in ()).throw(
                bootstrap.BootstrapError("configuration_check_failed")
            ),
            credential_loader=_test_credentials,
        )
    assert not bootstrap.APPROVED_STATE_DIRECTORY.exists()


@pytest.mark.asyncio
class _RollbackOnFailureIngress:
    def __init__(
        self,
        payload: bootstrap.Payload,
        before: bytes,
        after: bytes,
        installed_tree: str,
        installed_trust: str,
    ) -> None:
        self.payload = payload
        self.before = before
        self.after = after
        self.installed_tree = installed_tree
        self.installed_trust = installed_trust
        self.state = "prestate"
        self.staged: dict[str, bytes] = {}
        self.executions: list[str] = []

    def configuration_bytes(self) -> bytes:
        return self.before if self.state == "prestate" else self.after

    def component_tree_sha256(self) -> str:
        return _empty_component_digest() if self.state == "prestate" else self.installed_tree

    def trust_sha256(self) -> str:
        return "absent" if self.state == "prestate" else self.installed_trust

    def stage_exists(self, _txid: str) -> bool:
        return False

    def stage_file_exists(self, _txid: str, relative: str) -> bool:
        return relative in self.staged

    def create_stage(self, _txid: str) -> None:
        return None

    def verify_fixed_root_write_capability(self, _txid: str) -> None:
        return None

    def upload(self, _txid: str, relative: str, content: bytes) -> None:
        self.staged[relative] = content

    def download_stage(self, _txid: str, relative: str) -> bytes:
        return self.staged[relative]

    def execute(self, *, mode: str, transaction_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.executions.append(mode)
        installed = mode == "install"
        self.state = "installed" if installed else "prestate"
        return {
            "status": "installed" if installed else "rolled_back",
            "transactionId": transaction_id,
            "payloadManifestSha256": self.payload.manifest_sha256,
            "configurationSha256": hashlib.sha256(
                self.after if installed else self.before
            ).hexdigest(),
            "componentTreeSha256": self.installed_tree if installed else _empty_component_digest(),
            "trustSha256": self.installed_trust if installed else "absent",
        }


async def test_failed_config_check_uses_remote_transaction_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_root(tmp_path)
    pins = _pins(root)
    payload = _payload(root)
    txid = str(uuid.uuid4())
    before = b"default_config:\n"
    after = before + b"aurora_deploy:\n"
    installed_tree = hashlib.sha256(
        bootstrap._canonical(
            {
                "exists": True,
                "files": {
                    Path(item.relative_path).name: item.sha256
                    for item in payload.files
                    if item.relative_path.startswith("custom_components/")
                },
                "directories": [],
            }
        )
    ).hexdigest()
    installed_trust = next(
        item.sha256
        for item in payload.files
        if item.relative_path == bootstrap.TRUST_PATH
    )

    async def open_editor(*_args: Any) -> tuple[str, str, str]:
        return "started", "/api/hassio_ingress/fixed", "A" * 32

    monkeypatch.setattr(bootstrap, "_open_editor", open_editor)

    ingress = _RollbackOnFailureIngress(
        payload, before, after, installed_tree, installed_trust
    )
    checks = 0

    def check(*_args: Any) -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise bootstrap.BootstrapError("configuration_check_failed")

    with pytest.raises(bootstrap.BootstrapError, match="failed_rolled_back"):
        await bootstrap.bootstrap(
            mode="install",
            source_root=root,
            backup_id="backup-1",
            transaction_id=txid,
            expected_configuration_sha256=hashlib.sha256(before).hexdigest(),
            expected_component_tree_sha256=_empty_component_digest(),
            expected_trust_sha256="absent",
            expected_payload_manifest_sha256=pins["expected_manifest_sha256"],
            expected_release_key_sha256=pins["expected_release_key_sha256"],
            expected_validation_key_sha256=pins["expected_validation_key_sha256"],
            expected_source_revision=pins["expected_source_revision"],
            ingress_client_factory=lambda *_args: ingress,
            config_check=check,
            credential_loader=_test_credentials,
        )
    assert ingress.executions == ["install", "rollback"]
    assert ingress.state == "prestate"
    assert checks == 4


@pytest.mark.asyncio
async def test_primary_and_lifecycle_cleanup_failures_are_safely_combined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_root(tmp_path)

    async def open_editor(*_args: Any) -> tuple[str, str, str]:
        return "stopped", "/api/hassio_ingress/fixed", "A" * 32

    async def failed_cleanup(*_args: Any) -> None:
        raise RuntimeError("sensitive cleanup detail")

    monkeypatch.setattr(bootstrap, "_open_editor", open_editor)
    monkeypatch.setattr(bootstrap, "_addon_action", failed_cleanup)

    class Ingress:
        def __init__(self, *_args: Any) -> None:
            pass

        def status(self, _txid: str) -> dict[str, Any]:
            raise bootstrap.BootstrapError("transaction_invalid")

    with pytest.raises(
        bootstrap.BootstrapError,
        match="operation_failed_and_file_editor_restore_failed",
    ):
        await bootstrap.bootstrap(
            mode="status",
            source_root=root,
            transaction_id=str(uuid.uuid4()),
            ingress_client_factory=Ingress,
            credential_loader=_test_credentials,
        )


def test_cli_has_no_token_or_remote_path_command_url_arguments() -> None:
    help_text = bootstrap._parser().format_help()
    assert "--token" not in help_text
    assert "remote-path" not in help_text
    assert "--command" not in help_text
    assert "--home-assistant-url" not in help_text
