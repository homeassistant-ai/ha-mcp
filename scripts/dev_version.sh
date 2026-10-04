#!/usr/bin/env bash
# Print the version of the development build for the checked-out commit:
# <next release>.dev<commit count on origin/master>, e.g. 9.0.0.dev2901.
#
# <next release> is what semantic-release would cut from the commits since the
# last stable tag, so a dev build sorts between the release it follows and the
# one it previews, and the HACS pre-release component (which reports the plain
# <next release>) satisfies a MIN_COMPONENT_VERSION floor raised for that
# release. The commit count makes every surface (PyPI, Docker, the dev app and
# the HACS pre-release) report the same number for the same commit.
#
# Needs full history and tags (actions/checkout fetch-depth: 0).
set -euo pipefail

# renovate: datasource=pypi depName=python-semantic-release
PSR_VERSION="10.6.2"

next=$(uvx --quiet --from "python-semantic-release==${PSR_VERSION}" \
  semantic-release --noop version --print 2>/dev/null | tail -n 1)
if ! [[ "$next" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "dev_version.sh: semantic-release printed '${next}', not a version" >&2
  exit 1
fi
echo "${next}.dev$(git rev-list --count origin/master)"
