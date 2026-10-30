"""The registry the bake's local add-on builds pull ``python:3.13-slim`` from.

Supervisor builds a local add-on from its Dockerfile with no build args
(``build.yaml`` is no longer read, see
https://developers.home-assistant.io/docs/add-ons/configuration/), so the base
reference is rewritten in the staged copy of the Dockerfile. The project's GHCR
mirror (filled by ``mirror-python-base.yml`` with the same digests) is taken
when it serves the pinned digest to an anonymous client, as the VM pulls.
Otherwise ECR Public, whose anonymous data limit is shared by every GitHub
runner: on a 429 from the runner the same digest is taken from Docker Hub, and
any other answer keeps ECR; the bake's install retry covers a passing failure.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOG = logging.getLogger("haos_image_build")

ECR_PYTHON = "public.ecr.aws/docker/library/python"
DOCKERHUB_PYTHON = "docker.io/library/python"
MIRROR_REPOSITORY = "homeassistant-ai/ha-mcp-python-base"
MIRROR_PYTHON = f"ghcr.io/{MIRROR_REPOSITORY}"
_ECR_REF = re.compile(
    re.escape(ECR_PYTHON) + r":[\w.-]+@(?P<digest>sha256:[0-9a-f]{64})"
)
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
_TOKEN_URL = (
    "https://public.ecr.aws/token/?service=public.ecr.aws"
    "&scope=repository:docker/library/python:pull"
)
_MIRROR_TOKEN_URL = (
    f"https://ghcr.io/token?service=ghcr.io&scope=repository:{MIRROR_REPOSITORY}:pull"
)


def _get(url: str, headers: dict[str, str], timeout: float = 30) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, b""


def _manifest_status(token_url: str, manifest_url: str) -> int:
    status, body = _get(token_url, {})
    if status != 200:
        return status
    token = json.loads(body)["token"]
    status, _ = _get(
        manifest_url,
        {"Authorization": f"Bearer {token}", "Accept": _MANIFEST_ACCEPT},
    )
    return status


def ecr_manifest_status(digest: str) -> int:
    """HTTP status of the manifest fetch Docker makes first, the one that 429s."""
    return _manifest_status(
        _TOKEN_URL,
        f"https://public.ecr.aws/v2/docker/library/python/manifests/{digest}",
    )


def mirror_manifest_status(digest: str) -> int:
    """HTTP status of an anonymous fetch of the digest from the GHCR mirror."""
    return _manifest_status(
        _MIRROR_TOKEN_URL,
        f"https://ghcr.io/v2/{MIRROR_REPOSITORY}/manifests/{digest}",
    )


@functools.cache
def python_base_registry(digest: str) -> str:
    """``MIRROR_PYTHON`` when it serves the digest; else ``ECR_PYTHON`` unless
    ECR Public answers 429, then ``DOCKERHUB_PYTHON``."""
    try:
        mirror_status = mirror_manifest_status(digest)
    except (OSError, ValueError, KeyError) as err:
        mirror_status = None
        LOG.warning("GHCR mirror manifest probe failed (%r)", err)
    if mirror_status == 200:
        LOG.info("GHCR mirror serves %s; add-on builds pull %s", digest, MIRROR_PYTHON)
        return MIRROR_PYTHON
    if mirror_status is not None:
        LOG.info("GHCR mirror answered %s for %s", mirror_status, digest)
    try:
        status = ecr_manifest_status(digest)
    except (OSError, ValueError, KeyError) as err:
        LOG.warning(
            "ECR Public manifest probe failed (%r); keeping %s", err, ECR_PYTHON
        )
        return ECR_PYTHON
    if status == 429:
        LOG.warning(
            "ECR Public answered 429 for %s; add-on builds pull %s",
            digest,
            DOCKERHUB_PYTHON,
        )
        return DOCKERHUB_PYTHON
    LOG.info(
        "ECR Public answered %s for %s; add-on builds pull %s",
        status,
        digest,
        ECR_PYTHON,
    )
    return ECR_PYTHON


def rewrite_python_base(dockerfile_text: str) -> str:
    """The Dockerfile text with each ECR python reference moved to the chosen registry."""

    def _choose(match: re.Match[str]) -> str:
        registry = python_base_registry(match.group("digest"))
        return match.group(0).replace(ECR_PYTHON, registry, 1)

    return _ECR_REF.sub(_choose, dockerfile_text)


def copy_addon_tree(src: Path, dst: Path, **copytree_kwargs: Any) -> None:
    """``shutil.copytree`` plus the base-registry rewrite of the staged Dockerfile."""
    shutil.copytree(src, dst, **copytree_kwargs)
    dockerfile = dst / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    rewritten = rewrite_python_base(text)
    if rewritten != text:
        dockerfile.write_text(rewritten, encoding="utf-8")
        LOG.info("%s: python base rewritten off ECR Public", dockerfile)
