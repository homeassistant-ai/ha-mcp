"""The build arg that points a Docker test build at the registry CI could pull from."""

from __future__ import annotations

import os


def python_base_args() -> list[str]:
    """``--build-arg PYTHON_BASE=...`` when CI resolved the base image, else nothing.

    pr.yml's "Pull python base image" step exports ``PYTHON_BASE`` as the
    GHCR mirror, ECR Public or Docker Hub reference it managed to pull; without it the
    Dockerfile's own default applies.
    """
    base = os.environ.get("PYTHON_BASE")
    return ["--build-arg", f"PYTHON_BASE={base}"] if base else []
