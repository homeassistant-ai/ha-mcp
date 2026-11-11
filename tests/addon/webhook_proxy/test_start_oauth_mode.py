"""Which OAuth mode an enabled OAuth starts in: an upgrade never moves an
existing legacy user to ha_auth, which would break their connector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._start import App, make_app
from ._variants import Variant

LEGACY = "legacy"
HA_AUTH = "ha_auth"


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch)


def _mode(app: App, option: str, client_id: str = "", secret: str = "") -> str:
    mode: str = app.step("_resolve_oauth_mode")(
        option, client_id, secret, app.path("/data")
    )
    return mode


def _store_legacy_creds(app: App) -> None:
    app.path("/data/oauth_creds.json").write_text(
        json.dumps({"client_id": "x", "client_secret": "y"})
    )


def test_explicit_ha_auth_wins_over_legacy_traces(app: App) -> None:
    _store_legacy_creds(app)

    assert _mode(app, HA_AUTH, "cid-1234567890ABCDEF", "sec") == HA_AUTH


def test_explicit_legacy_wins_on_a_first_enable(app: App) -> None:
    assert _mode(app, LEGACY) == LEGACY


def test_stored_legacy_credentials_keep_legacy(app: App) -> None:
    _store_legacy_creds(app)

    assert _mode(app, "") == LEGACY


@pytest.mark.parametrize(
    ("client_id", "secret"),
    [("cid-1234567890ABCDEF", ""), ("", "sec")],
    ids=["client-id", "client-secret"],
)
def test_a_configured_legacy_credential_keeps_legacy(
    app: App, client_id: str, secret: str
) -> None:
    assert _mode(app, "", client_id, secret) == LEGACY


def test_first_enable_defaults_to_ha_auth(app: App) -> None:
    assert _mode(app, "") == HA_AUTH


def test_unknown_mode_falls_back_to_detection(app: App) -> None:
    """The Supervisor's enum check normally blocks it; if one gets through,
    startup must not crash."""
    assert _mode(app, "bogus") == HA_AUTH
    assert _mode(app, "bogus", "cid-1234567890ABCDEF", "sec") == LEGACY


def test_unknowable_stored_credentials_keep_legacy(
    app: App, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the creds file cannot be checked (a stat error other than "not
    there"), its absence is not proven: defaulting to ha_auth could break an
    existing legacy connector, so the previous (legacy) flow is kept."""
    # A directory name past the filesystem's limit: stat() of anything in it
    # fails with ENAMETOOLONG for any user, root included.
    unknowable = tmp_path / ("d" * 300)

    mode = app.step("_resolve_oauth_mode")("", "", "", unknowable)

    assert mode == LEGACY
    err = capsys.readouterr().err
    assert "indeterminate" in err
    assert "Preserving the previous (legacy) OAuth flow" in err
