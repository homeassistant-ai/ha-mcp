# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Expose a stable release only after its PyPI pin and requested ZIP are ready."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _gh_json(endpoint: str, *, method: str = "GET", fields: dict | None = None) -> Any:
    command = ["gh", "api", "--method", method, endpoint]
    if fields is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(fields) if fields is not None else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _pypi_ready(version: str) -> bool:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/ha-mcp/{urllib.parse.quote(version, safe='')}/json",
        headers={"User-Agent": "ha-mcp-release-finalizer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return bool(json.load(response).get("info", {}).get("version") == version)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def _required_zip(hacs: dict, component: dict, version: str) -> str | None:
    if not hacs.get("zip_release"):
        return None  # Recovery of pre-ZIP releases retains archive installation.
    filename = hacs.get("filename")
    if not isinstance(filename, str) or not filename.endswith(".zip"):
        raise ValueError("zip_release requires a ZIP filename")
    pins = [
        r
        for r in component.get("requirements", [])
        if r.startswith(("ha-mcp==", "ha-mcp-dev=="))
    ]
    if component.get("version") != version or pins != [f"ha-mcp=={version}"]:
        raise ValueError(
            "The stable component version and server pin must match the release"
        )
    return filename


def _wait_for_pypi(version: str, attempts: int, delay: float) -> None:
    for attempt in range(attempts):
        if _pypi_ready(version):
            return
        if attempt + 1 < attempts:
            print(f"Waiting for ha-mcp=={version} on PyPI ({attempt + 1}/{attempts})")
            time.sleep(delay)
    raise TimeoutError(f"PyPI does not serve ha-mcp=={version}; release stays draft")


def finalize_release(
    repository: str,
    tag: str,
    hacs: dict,
    component: dict,
    *,
    attempts: int = 20,
    delay: float = 10,
) -> dict:
    version = tag.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Only a stable semantic-version tag can be finalized")
    filename = _required_zip(hacs, component, version)
    _wait_for_pypi(version, attempts, delay)

    release = _gh_json(
        f"repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    )
    if release.get("tag_name") != tag or release.get("prerelease"):
        raise ValueError("The requested stable release was not found")
    if filename is not None:
        asset: dict = next(
            (a for a in release.get("assets", []) if a.get("name") == filename), {}
        )
        if (
            asset.get("state") != "uploaded"
            or not asset.get("size")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or "")
        ):
            raise ValueError("The integration ZIP is not ready; release stays draft")

    target = _gh_json(
        f"repos/{repository}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    )["object"]["sha"]
    refs = _gh_json(f"repos/{repository}/git/matching-refs/tags/stable")
    current = next((r for r in refs if r["ref"] == "refs/tags/stable"), None)
    moved = current is None or current["object"]["sha"] != target
    if current is not None and moved:
        comparison = _gh_json(
            f"repos/{repository}/compare/stable...{urllib.parse.quote(tag, safe='')}"
        )
        if comparison.get("status") not in {"ahead", "identical"}:
            raise ValueError("The release must advance stable; refusing an old retry")
    # A failed publication leaves the previous public stable alias usable.
    if release.get("draft"):
        _gh_json(
            f"repos/{repository}/releases/{release['id']}",
            method="PATCH",
            fields={"draft": False},
        )
    _update_stable_ref(repository, target, current, moved)
    return {
        "tag": tag,
        "zip_required": filename is not None,
        "stable_ref_updated": moved,
    }


def _update_stable_ref(
    repository: str, target: str, current: dict | None, moved: bool
) -> None:
    if current is None:
        _gh_json(
            f"repos/{repository}/git/refs",
            method="POST",
            fields={"ref": "refs/tags/stable", "sha": target},
        )
    elif moved:
        _gh_json(
            f"repos/{repository}/git/refs/tags/stable",
            method="PATCH",
            fields={"sha": target, "force": True},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--hacs", type=Path, required=True)
    parser.add_argument("--component", type=Path, required=True)
    args = parser.parse_args()
    result = finalize_release(
        args.repository,
        args.tag,
        json.loads(args.hacs.read_text(encoding="utf-8")),
        json.loads(args.component.read_text(encoding="utf-8")),
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
