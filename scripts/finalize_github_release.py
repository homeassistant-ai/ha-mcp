# /// script
# requires-python = ">=3.11"
# dependencies = ["packaging>=24.2,<27"]
# ///
"""Expose a stable release only after its PyPI pin and requested ZIP are ready."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import Version


def _gh_json(
    endpoint: str,
    *,
    method: str = "GET",
    fields: dict | None = None,
    paginate: bool = False,
) -> Any:
    command = ["gh", "api", "--method", method, endpoint]
    if paginate:
        command += ["--paginate", "--slurp"]
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


def _supported_pythons(project: dict) -> tuple[str, ...]:
    supported = SpecifierSet(project["requires-python"])
    versions = tuple(
        match[1]
        for classifier in project["classifiers"]
        if (
            match := re.fullmatch(
                r"Programming Language :: Python :: (\d+\.\d+)", classifier
            )
        )
        and Version(match[1]) in supported
    )
    if not versions:
        raise ValueError(
            "The release project must declare its supported Python versions"
        )
    return versions


def _wheel_pythons(file: dict, version: str, pythons: tuple[str, ...]) -> set[str]:
    if (
        file.get("packagetype") != "bdist_wheel"
        or file.get("yanked")
        or not file.get("size")
    ):
        return set()
    try:
        name, found_version, _, tags = parse_wheel_filename(file["filename"])
        requires = SpecifierSet(file.get("requires_python") or "")
    except (KeyError, InvalidWheelFilename, InvalidSpecifier):
        return set()
    if name != "ha-mcp" or found_version != Version(version):
        return set()
    # HA-MCP publishes a pure Python wheel: it must cover both supported HA CPUs.
    return {
        python
        for python in pythons
        if Version(python) in requires
        and any(
            t.platform == "any"
            and t.abi == "none"
            and t.interpreter in {"py3", "py" + python.replace(".", "")}
            for t in tags
        )
    }


def _distribution_available(file: dict) -> bool:
    url = urllib.parse.urlsplit(file.get("url", ""))
    if url.scheme != "https" or url.netloc != "files.pythonhosted.org":
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", file.get("digests", {}).get("sha256", "")):
        return False
    request = urllib.request.Request(file["url"], method="HEAD")
    with urllib.request.urlopen(request, timeout=10) as response:
        return bool(
            response.status == 200
            and int(response.headers.get("Content-Length", 0)) == file["size"]
        )


def _pypi_ready(version: str, pythons: tuple[str, ...]) -> bool:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/ha-mcp/{urllib.parse.quote(version, safe='')}/json",
        headers={"User-Agent": "ha-mcp-release-finalizer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            metadata = json.load(response)
        if metadata.get("info", {}).get("version") != version:
            return False
        covered: set[str] = set()
        for file in metadata.get("urls", []):
            usable = _wheel_pythons(file, version, pythons)
            if usable and _distribution_available(file):
                covered.update(usable)
        return covered == set(pythons)
    except (urllib.error.URLError, OSError, ValueError):
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


def _wait_for_pypi(
    version: str, pythons: tuple[str, ...], attempts: int, delay: float
) -> None:
    for attempt in range(attempts):
        if _pypi_ready(version, pythons):
            return
        if attempt + 1 < attempts:
            print(f"Waiting for ha-mcp=={version} on PyPI ({attempt + 1}/{attempts})")
            time.sleep(delay)
    raise TimeoutError(
        f"PyPI is not ready for ha-mcp=={version}; no GitHub writes performed"
    )


def _commit_sha(repository: str, git_object: dict) -> str:
    while git_object["type"] == "tag":
        git_object = _gh_json(f"repos/{repository}/git/tags/{git_object['sha']}")[
            "object"
        ]
    if git_object["type"] != "commit":
        raise ValueError("The stable and version tags must resolve to commits")
    return str(git_object["sha"])


def _verify_release_zip(
    repository: str, asset: dict, checkout: Path, commit: str
) -> None:
    """Bind the actual uploaded bytes to the tagged component before publication."""
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "diff",
                "--exit-code",
                commit,
                "--",
                "custom_components/ha_mcp_tools",
            ],
            check=True,
            capture_output=True,
        )
        with tempfile.TemporaryDirectory(prefix="ha-mcp-release-zip-") as temporary:
            archive = Path(temporary) / "ha_mcp_tools.zip"
            with archive.open("wb") as target:
                subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/{repository}/releases/assets/{asset['id']}",
                        "--header",
                        "Accept: application/octet-stream",
                    ],
                    stdout=target,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("build_component_zip.py")),
                    "--component-dir",
                    str(checkout / "custom_components/ha_mcp_tools"),
                    "--verify",
                    str(archive),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.stdout.strip() != asset["digest"]:
                raise ValueError(
                    "The downloaded integration ZIP digest differs from GitHub metadata"
                )
    except subprocess.CalledProcessError as error:
        raise ValueError(
            "The integration ZIP could not be verified against the tagged component"
        ) from error


def finalize_release(
    repository: str,
    tag: str,
    hacs: dict,
    component: dict,
    *,
    project: dict,
    checkout: Path = Path("."),
    attempts: int = 20,
    delay: float = 10,
) -> dict:
    version = tag.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Only a stable semantic-version tag can be finalized")
    filename = _required_zip(hacs, component, version)
    _wait_for_pypi(version, _supported_pythons(project), attempts, delay)

    # The tag endpoint only guarantees published releases. Authenticated release
    # lists also contain drafts; paginate without a fixed release-count limit.
    pages = _gh_json(f"repos/{repository}/releases?per_page=100", paginate=True)
    release: dict = next(
        (r for page in pages for r in page if r.get("tag_name") == tag), {}
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
            raise ValueError(
                "The integration ZIP is not ready; no GitHub writes performed"
            )

    target_object = _gh_json(
        f"repos/{repository}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    )["object"]
    target = target_object["sha"]
    if filename is not None:
        _verify_release_zip(
            repository, asset, checkout, _commit_sha(repository, target_object)
        )
    refs = _gh_json(f"repos/{repository}/git/matching-refs/tags/stable")
    current = next((r for r in refs if r["ref"] == "refs/tags/stable"), None)
    moved = current is None or current["object"]["sha"] != target
    if current is not None and moved:
        old_commit = _commit_sha(repository, current["object"])
        new_commit = _commit_sha(repository, target_object)
        comparison = _gh_json(f"repos/{repository}/compare/{old_commit}...{new_commit}")
        if comparison.get("status") not in {"ahead", "identical"}:
            raise ValueError("The release must advance stable; refusing an old retry")
    # A failed publication leaves the previous public stable alias usable.
    if release.get("draft"):
        _gh_json(
            f"repos/{repository}/releases/{release['id']}",
            method="PATCH",
            fields={"draft": False},
        )
    if moved:
        expected = current["object"]["sha"] if current is not None else ""
        _push_stable_ref(
            f"https://github.com/{repository}.git", checkout, target, expected
        )
    return {
        "tag": tag,
        "zip_required": filename is not None,
        "stable_ref_updated": moved,
    }


def _push_stable_ref(remote: str, checkout: Path, target: str, expected: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "push",
            f"--force-with-lease=refs/tags/stable:{expected}",
            "--",
            remote,
            f"{target}:refs/tags/stable",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--hacs", type=Path, required=True)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    args = parser.parse_args()
    result = finalize_release(
        args.repository,
        args.tag,
        json.loads(args.hacs.read_text(encoding="utf-8")),
        json.loads(args.component.read_text(encoding="utf-8")),
        project=tomllib.loads(args.pyproject.read_text(encoding="utf-8"))["project"],
        checkout=args.checkout,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
