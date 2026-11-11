"""The secrets the app keeps across restarts: OAuth client credentials, the
webhook id, the signing key and the proxy config handed to the component."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from types import ModuleType

import pytest

from ._start import App, file_mode, make_app, no_restricted_create_under
from ._variants import Variant


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch)


def _resolve(app: App, client_id: str = "", secret: str = "") -> tuple[str, str]:
    creds: tuple[str, str] = app.step("_resolve_oauth_creds")(
        app.path("/data"), client_id, secret
    )
    return creds


def _store_creds(app: App, client_id: str, secret: str) -> None:
    app.path("/data/oauth_creds.json").write_text(
        json.dumps({"client_id": client_id, "client_secret": secret})
    )


def test_configured_credentials_are_used_trimmed(app: App) -> None:
    assert _resolve(app, "  user-supplied-id-1234567890  ", "  pw  ") == (
        "user-supplied-id-1234567890",
        "pw",
    )


def test_blank_credentials_are_generated_strong_enough_to_start(app: App) -> None:
    """A generated client id passes main()'s 16-character floor and the secret
    carries 256 bits (43 base64url characters)."""
    client_id, secret = _resolve(app)

    assert len(client_id) >= 16
    assert len(secret) >= 43


def test_generated_credentials_survive_a_restart(app: App) -> None:
    """A connector set up with generated credentials keeps working after the
    app restarts."""
    assert _resolve(app) == _resolve(app)


def test_configured_credentials_replace_the_stored_ones(app: App) -> None:
    _resolve(app)

    assert _resolve(app, "user-id-1234567890123", "user-secret") == (
        "user-id-1234567890123",
        "user-secret",
    )
    # A later start with the fields cleared keeps what the user last set.
    assert _resolve(app) == ("user-id-1234567890123", "user-secret")


def test_rotating_only_the_secret_keeps_the_stored_client_id(app: App) -> None:
    client_id, _secret = _resolve(app)

    assert _resolve(app, "", "rotated-secret") == (client_id, "rotated-secret")


def test_unreadable_stored_credentials_are_replaced_with_a_warning(
    app: App, capsys: pytest.CaptureFixture[str]
) -> None:
    app.path("/data/oauth_creds.json").write_text("not valid json{{{")

    client_id, secret = _resolve(app)

    assert len(client_id) >= 16
    assert len(secret) >= 43
    assert "Could not read existing OAuth creds" in capsys.readouterr().err


def test_stored_credentials_file_is_private(app: App) -> None:
    _resolve(app)

    assert file_mode(app.path("/data/oauth_creds.json")) == 0o600


def test_credentials_persist_with_a_warning_where_0600_is_unavailable(
    app: App, capsys: pytest.CaptureFixture[str]
) -> None:
    """tmpfs or a non-POSIX filesystem cannot honour a restricted create; the
    credentials are still kept, and the wider permissions are reported."""
    with no_restricted_create_under(app.fs_root):
        client_id, secret = _resolve(app)

    assert len(client_id) >= 16
    assert _resolve(app) == (client_id, secret)
    assert "wider permissions than intended" in capsys.readouterr().err


def test_regenerate_gives_new_credentials(app: App) -> None:
    _store_creds(app, "old-id-1234567890", "old-secret")

    app.step("_regenerate_oauth_creds")(app.path("/data"))
    client_id, secret = _resolve(app)

    assert client_id != "old-id-1234567890"
    assert secret != "old-secret"


def test_regenerate_without_stored_credentials_is_harmless(app: App) -> None:
    app.step("_regenerate_oauth_creds")(app.path("/data"))

    assert not app.path("/data/oauth_creds.json").exists()


def test_regenerate_toggle_is_switched_off_keeping_other_options(app: App) -> None:
    """Left on, the toggle would regenerate the credentials on every restart."""
    cleared = app.step("_clear_regenerate_toggle")(
        {"remote_url": "https://example.com", "regenerate_oauth_creds": True}
    )

    assert cleared is True
    assert app.supervisor.posts == [
        (
            "/addons/self/options",
            {
                "options": {
                    "remote_url": "https://example.com",
                    "regenerate_oauth_creds": False,
                }
            },
        )
    ]


def test_webhook_id_is_generated_once_and_kept(app: App) -> None:
    """The webhook URL the user pasted into their MCP client must not change
    across restarts."""
    get_id = app.step("_get_or_create_webhook_id")
    webhook_id = get_id(app.path("/data"))

    assert len(webhook_id) > 10
    assert get_id(app.path("/data")) == webhook_id


def test_webhook_id_from_an_earlier_install_is_kept(app: App) -> None:
    app.path("/data/webhook_id.txt").write_text("mcp_existing_test_id_12345")

    webhook_id = app.step("_get_or_create_webhook_id")(app.path("/data"))

    assert webhook_id == "mcp_existing_test_id_12345"


def test_empty_webhook_id_file_gets_a_new_id(app: App) -> None:
    app.path("/data/webhook_id.txt").write_text("")
    get_id = app.step("_get_or_create_webhook_id")

    webhook_id = get_id(app.path("/data"))

    assert len(webhook_id) > 10
    assert get_id(app.path("/data")) == webhook_id


def test_proxy_config_handed_to_the_component_is_private(app: App) -> None:
    """The proxy config carries the OAuth client secret when legacy OAuth is on."""
    config = {"target_url": "http://127.0.0.1:9583/private_x", "webhook_id": "w"}

    assert app.step("_write_proxy_config")(config) is True

    written = app.path(app.variant.config_file)
    assert json.loads(written.read_text()) == config
    assert file_mode(written) == 0o600


def test_proxy_config_is_written_with_a_warning_where_0600_is_unavailable(
    app: App, capsys: pytest.CaptureFixture[str]
) -> None:
    config = {"target_url": "http://127.0.0.1:9583/private_x", "webhook_id": "w"}

    with no_restricted_create_under(app.fs_root):
        assert app.step("_write_proxy_config")(config) is True

    assert json.loads(app.path(app.variant.config_file).read_text()) == config
    assert "Could not create the proxy config file" in capsys.readouterr().err


def _oauth(component: ModuleType) -> ModuleType:
    return importlib.import_module(f"{component.__name__}.oauth")


def test_signing_key_file_is_private(component: ModuleType) -> None:
    oauth = _oauth(component)

    oauth.load_or_create_secret()

    assert file_mode(oauth.SECRET_FILE) == 0o600


def test_regenerated_signing_key_file_is_made_private_again(
    component: ModuleType,
) -> None:
    """A short (invalid) key left world-readable is regenerated, and the
    regenerated file is 0600: an overwrite in place would keep the old mode."""
    oauth = _oauth(component)
    oauth.SECRET_FILE.write_bytes(b"short")
    oauth.SECRET_FILE.chmod(0o644)

    oauth.load_or_create_secret()

    assert file_mode(oauth.SECRET_FILE) == 0o600


def test_signing_key_persists_with_a_warning_where_0600_is_unavailable(
    component: ModuleType, fs_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    oauth = _oauth(component)

    with (
        no_restricted_create_under(fs_root),
        caplog.at_level(logging.WARNING, logger=oauth.__name__),
    ):
        secret = oauth.load_or_create_secret()

    assert len(secret) == 32
    assert oauth.SECRET_FILE.read_bytes() == secret
    assert any("restricted permissions" in r.getMessage() for r in caplog.records)
