"""Unit coverage for ``scripts/generate_locales.py``'s CLI and error paths.

The happy path — committed derived catalogs equal generator output on the
real repo data — is covered by
``test_locale_parity.py::test_derived_catalogs_match_the_canonical_store``.
What lives here is the plumbing CI and contributors actually invoke: the
``--check`` exit codes and the error a contributor hits when an add-on option
has no canonical string.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import generate_locales  # noqa: E402


class TestResolveText:
    def test_override_order_is_flavor_then_features_then_addon(self) -> None:
        messages = {
            "addon_stable.opt.description": "stable wording",
            "features.opt.help": "shared wording",
            "addon.opt.description": "addon wording",
        }
        assert (
            generate_locales.resolve_text(messages, {}, "stable", "opt", "description")
            == "stable wording"
        )
        del messages["addon_stable.opt.description"]
        assert (
            generate_locales.resolve_text(messages, {}, "stable", "opt", "description")
            == "shared wording"
        )
        del messages["features.opt.help"]
        assert (
            generate_locales.resolve_text(messages, {}, "stable", "opt", "description")
            == "addon wording"
        )

    def test_locale_falls_back_to_english(self) -> None:
        english = {"addon.opt.name": "English name"}
        assert (
            generate_locales.resolve_text({}, english, "dev", "opt", "name")
            == "English name"
        )

    def test_missing_canonical_string_names_the_key_to_add(self) -> None:
        with pytest.raises(SystemExit, match=r"addon\.opt\.name"):
            generate_locales.resolve_text({}, {}, "dev", "opt", "name")


class TestCheckCli:
    def test_check_passes_on_the_committed_tree(self) -> None:
        # The same guarantee as the parity test, but through the entry point
        # the CI step and contributors actually run.
        assert generate_locales.check() == 0

    def test_check_fails_naming_a_stale_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stale = tmp_path / "stale.yaml"
        stale.write_text("old\n", encoding="utf-8")
        monkeypatch.setattr(generate_locales, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            generate_locales, "generated_files", lambda: {stale: "new\n"}
        )
        assert generate_locales.check() == 1
        assert "stale.yaml" in capsys.readouterr().err

    def test_main_routes_the_check_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            generate_locales, "check", lambda: calls.append("check") or 0
        )
        monkeypatch.setattr(
            generate_locales, "write", lambda: calls.append("write") or 0
        )
        monkeypatch.setattr(sys, "argv", ["generate_locales.py", "--check"])
        assert generate_locales.main() == 0
        monkeypatch.setattr(sys, "argv", ["generate_locales.py"])
        assert generate_locales.main() == 0
        assert calls == ["check", "write"]
