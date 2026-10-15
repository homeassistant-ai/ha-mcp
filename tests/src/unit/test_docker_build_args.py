"""The Docker test builds pass CI's resolved base image through, and only then."""

from __future__ import annotations

import pytest

from tests.addon.docker_build import python_base_args


def test_resolved_base_image_becomes_a_build_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHON_BASE", "docker.io/library/python:3.13-slim@sha256:abc")

    assert python_base_args() == [
        "--build-arg",
        "PYTHON_BASE=docker.io/library/python:3.13-slim@sha256:abc",
    ]


@pytest.mark.parametrize("value", [None, ""])
def test_without_a_resolved_base_the_dockerfile_default_applies(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("PYTHON_BASE", raising=False)
    else:
        monkeypatch.setenv("PYTHON_BASE", value)

    assert python_base_args() == []
