"""Unit tests for scripts/redact_diagnostics_secrets.py.

The HAOS diagnostics artifact tars ``.storage`` out of the booted qcow2;
since CI injects GITHUB_TOKEN into the HACS config entry pre-boot, the tar
would otherwise persist a credential into a downloadable artifact. The
script must redact credential values in place and FAIL CLOSED (delete the
tar) when it cannot guarantee redaction.
"""

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "redact_diagnostics_secrets.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "redact_diagnostics_secrets", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


redact = _load()


def _make_storage_tar(
    path: Path, config_entries: dict, extra_files: dict | None = None
):
    with tarfile.open(path, "w") as tf:
        payload = json.dumps(config_entries).encode()
        info = tarfile.TarInfo("./core.config_entries")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
        for name, content in (extra_files or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))


def _doc(entries):
    return {"version": 1, "data": {"entries": entries}}


def test_redacts_hacs_token_and_preserves_other_members(tmp_path):
    tar = tmp_path / "storage.tar"
    _make_storage_tar(
        tar,
        _doc(
            [
                {"domain": "hacs", "data": {"token": "ghs_secret_value"}},
                {"domain": "sun", "data": {}},
            ]
        ),
        extra_files={"./hacs.data": b'{"repositories": {}}'},
    )
    assert redact.redact_storage_tar(tar) == 1
    with tarfile.open(tar) as tf:
        doc = json.loads(tf.extractfile("./core.config_entries").read())
        assert doc["data"]["entries"][0]["data"]["token"] == "**REDACTED**"
        assert "ghs_secret_value" not in json.dumps(doc)
        # Non-config-entries members ride through byte-identical.
        assert tf.extractfile("./hacs.data").read() == b'{"repositories": {}}'


def test_redacts_all_credential_keys_across_entries(tmp_path):
    tar = tmp_path / "storage.tar"
    _make_storage_tar(
        tar,
        _doc(
            [
                {"domain": "a", "data": {"password": "p", "host": "h"}},
                {"domain": "b", "data": {"client_secret": "s", "api_key": "k"}},
            ]
        ),
    )
    assert redact.redact_storage_tar(tar) == 3
    with tarfile.open(tar) as tf:
        doc = json.loads(tf.extractfile("./core.config_entries").read())
    a, b = doc["data"]["entries"]
    assert a["data"] == {"password": "**REDACTED**", "host": "h"}
    assert b["data"] == {"client_secret": "**REDACTED**", "api_key": "**REDACTED**"}


def test_main_fail_closed_deletes_unparseable_tar(tmp_path, capsys):
    bad = tmp_path / "sub" / "storage.tar"
    bad.parent.mkdir()
    bad.write_bytes(b"this is not a tar archive")
    assert redact.main(["prog", str(tmp_path)]) == 0
    assert not bad.exists(), "unredactable tar must be deleted, not uploaded"
    assert "DELETED" in capsys.readouterr().err


def test_main_missing_root_is_noop(tmp_path):
    assert redact.main(["prog", str(tmp_path / "nope")]) == 0


def test_main_redacts_recursively(tmp_path):
    for gw in ("gw0", "gw1"):
        d = tmp_path / f"haos-test-image-{gw}"
        d.mkdir()
        _make_storage_tar(
            d / "storage.tar", _doc([{"domain": "hacs", "data": {"token": "x"}}])
        )
    assert redact.main(["prog", str(tmp_path)]) == 0
    for gw in ("gw0", "gw1"):
        with tarfile.open(tmp_path / f"haos-test-image-{gw}" / "storage.tar") as tf:
            doc = json.loads(tf.extractfile("./core.config_entries").read())
        assert doc["data"]["entries"][0]["data"]["token"] == "**REDACTED**"
