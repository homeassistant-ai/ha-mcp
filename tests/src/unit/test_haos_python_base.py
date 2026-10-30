"""The HAOS bake's local add-on builds take python:3.13-slim from the
project's GHCR mirror when it serves the pinned digest, else from Docker Hub
when ECR Public answers 429, decided once on the runner before staging."""

from __future__ import annotations

import urllib.error
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.haos_image_build import python_base

_DIGEST = "sha256:" + "ab" * 32
_ECR_REF = f"{python_base.ECR_PYTHON}:3.13-slim@{_DIGEST}"
_HUB_REF = f"{python_base.DOCKERHUB_PYTHON}:3.13-slim@{_DIGEST}"
_MIRROR_REF = f"{python_base.MIRROR_PYTHON}:3.13-slim@{_DIGEST}"
_ARG_FORM = f"ARG PYTHON_BASE={_ECR_REF}\nFROM ${{PYTHON_BASE}}\nCOPY x /\n"
_FROM_FORM = f"FROM {_ECR_REF}\nCOPY x /\n"
# Taken before the autouse fixture replaces it, for the probe's own test.
_REAL_MIRROR_STATUS = python_base.mirror_manifest_status


@pytest.fixture(autouse=True)
def _fresh_decision() -> None:
    python_base.python_base_registry.cache_clear()


@pytest.fixture(autouse=True)
def _mirror_without_the_digest() -> Iterator[MagicMock]:
    # The ECR cases below describe a mirror that has not copied the pin yet.
    with patch.object(python_base, "mirror_manifest_status", return_value=404) as m:
        yield m


def test_the_mirror_serving_the_digest_is_used_without_asking_ecr(
    _mirror_without_the_digest: MagicMock,
) -> None:
    _mirror_without_the_digest.return_value = 200
    ecr = MagicMock(return_value=429)
    with patch.object(python_base, "ecr_manifest_status", ecr):
        assert python_base.rewrite_python_base(_ARG_FORM) == _ARG_FORM.replace(
            _ECR_REF, _MIRROR_REF
        )
    ecr.assert_not_called()


def test_a_failed_mirror_probe_falls_back_to_the_ecr_decision(
    _mirror_without_the_digest: MagicMock,
) -> None:
    _mirror_without_the_digest.side_effect = urllib.error.URLError("no route")
    with patch.object(python_base, "ecr_manifest_status", return_value=429):
        assert python_base.rewrite_python_base(_ARG_FORM) == _ARG_FORM.replace(
            _ECR_REF, _HUB_REF
        )


@pytest.mark.parametrize("text", [_ARG_FORM, _FROM_FORM], ids=["arg", "from"])
def test_a_429_from_ecr_moves_the_pin_to_docker_hub(text: str) -> None:
    with patch.object(python_base, "ecr_manifest_status", return_value=429):
        assert python_base.rewrite_python_base(text) == text.replace(_ECR_REF, _HUB_REF)


@pytest.mark.parametrize("status", [200, 500, 403])
def test_any_other_answer_keeps_ecr(status: int) -> None:
    with patch.object(python_base, "ecr_manifest_status", return_value=status):
        assert python_base.rewrite_python_base(_ARG_FORM) == _ARG_FORM


def test_a_failed_probe_keeps_ecr(caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(
        python_base,
        "ecr_manifest_status",
        side_effect=urllib.error.URLError("no route"),
    ):
        assert python_base.rewrite_python_base(_ARG_FORM) == _ARG_FORM
    assert "keeping public.ecr.aws" in caplog.text


def test_the_decision_is_made_once_per_digest() -> None:
    probe = MagicMock(return_value=429)
    with patch.object(python_base, "ecr_manifest_status", probe):
        python_base.rewrite_python_base(_ARG_FORM)
        python_base.rewrite_python_base(_FROM_FORM)
    assert probe.call_count == 1


def test_the_probe_fetches_the_manifest_docker_fetches_first() -> None:
    """The request that returned 429 in the failed build was the manifest GET."""
    token = MagicMock()
    token.__enter__.return_value.status = 200
    token.__enter__.return_value.read.return_value = b'{"token": "t0k"}'
    manifest = MagicMock()
    manifest.__enter__.return_value.status = 429
    with patch.object(
        python_base.urllib.request, "urlopen", side_effect=[token, manifest]
    ) as urlopen:
        assert python_base.ecr_manifest_status(_DIGEST) == 429
    request = urlopen.call_args_list[1].args[0]
    assert request.full_url == (
        f"https://public.ecr.aws/v2/docker/library/python/manifests/{_DIGEST}"
    )
    assert request.get_header("Authorization") == "Bearer t0k"
    assert "manifest.list.v2+json" in request.get_header("Accept")


def test_the_mirror_probe_asks_ghcr_anonymously_for_the_pinned_manifest() -> None:
    """A private or not-yet-copied mirror must read as "not serving" from the
    same anonymous position the VM pulls from."""
    token = MagicMock()
    token.__enter__.return_value.status = 200
    token.__enter__.return_value.read.return_value = b'{"token": "t0k"}'
    manifest = MagicMock()
    manifest.__enter__.return_value.status = 200
    with patch.object(
        python_base.urllib.request, "urlopen", side_effect=[token, manifest]
    ) as urlopen:
        assert _REAL_MIRROR_STATUS(_DIGEST) == 200
    token_request, manifest_request = (c.args[0] for c in urlopen.call_args_list)
    assert token_request.get_header("Authorization") is None
    token_url = urlsplit(token_request.full_url)
    assert token_url.netloc == "ghcr.io"
    repository = (
        urlsplit(manifest_request.full_url)
        .path.removeprefix("/v2/")
        .partition("/manifests/")[0]
    )
    assert parse_qs(token_url.query)["scope"] == [f"repository:{repository}:pull"]
    assert manifest_request.full_url == (
        f"https://ghcr.io/v2/{python_base.MIRROR_REPOSITORY}/manifests/{_DIGEST}"
    )
    assert manifest_request.get_header("Authorization") == "Bearer t0k"


def test_copy_addon_tree_rewrites_only_the_staged_dockerfile(tmp_path: Path) -> None:
    src = tmp_path / "addon"
    src.mkdir()
    (src / "Dockerfile").write_text(_ARG_FORM, encoding="utf-8")
    (src / "config.yaml").write_text("name: x\n", encoding="utf-8")
    with patch.object(python_base, "ecr_manifest_status", return_value=429):
        python_base.copy_addon_tree(src, tmp_path / "staged")
    assert (tmp_path / "staged" / "Dockerfile").read_text(
        encoding="utf-8"
    ) == _ARG_FORM.replace(_ECR_REF, _HUB_REF)
    assert (src / "Dockerfile").read_text(encoding="utf-8") == _ARG_FORM
    assert (tmp_path / "staged" / "config.yaml").exists()
