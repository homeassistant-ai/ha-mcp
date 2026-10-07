#!/usr/bin/env bash
# Print the version of the development build for the checked-out commit:
# <next release>.dev<commit count>, e.g. 9.0.0.dev2901.
#
# <next release> is what semantic-release would cut from the commits since the
# last stable tag, so a dev build sorts between the release it follows and the
# one it previews, and the HACS pre-release component (which reports the plain
# <next release>) satisfies a MIN_COMPONENT_VERSION floor raised for that
# release. The commit count makes every surface (PyPI, Docker, the dev app and
# the HACS pre-release) report the same number for the same commit.
#
# A push build counts its own commit. origin/master may already hold a later
# push, and two builds sharing a number would make PyPI (skip-existing) keep the
# older build, while the newer build and the HACS pre-release pinning that
# number report it as theirs. A manual dispatch counts origin/master instead, so
# a dispatch on an older ref never mints a number an earlier build already used.
#
# Needs full history and tags (actions/checkout fetch-depth: 0) and an attached
# branch: semantic-release refuses to compute a version on a detached HEAD.
set -euo pipefail

if [ "${GITHUB_EVENT_NAME:-}" = push ]; then
  ref=HEAD
else
  ref=origin/master
fi

# renovate: datasource=pypi depName=python-semantic-release
PSR_VERSION="10.7.0"

# semantic-release logs to stderr; keep it for the failure messages only.
psr_log=$(mktemp)
trap 'rm -f "$psr_log"' EXIT
if ! next=$(uvx --quiet --from "python-semantic-release==${PSR_VERSION}" \
  semantic-release --noop version --print 2>"$psr_log" | tail -n 1); then
  echo "dev_version.sh: semantic-release failed:" >&2
  cat "$psr_log" >&2
  exit 1
fi
if ! [[ "$next" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "dev_version.sh: semantic-release printed '${next}', not a version" >&2
  cat "$psr_log" >&2
  exit 1
fi
echo "${next}.dev$(git rev-list --count "$ref")"
