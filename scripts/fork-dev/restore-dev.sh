#!/usr/bin/env bash
# Revert the dev add-on directory back to whatever git has on the
# current branch — undoes a previous `scripts/fork-dev/copy-stable.sh`
# run.
#
# This is the safe way to swap back to the dev flavour: it uses
# `git checkout --` instead of trying to invert each sed/python patch
# (which would silently drift if stable's schema changed in the
# meantime).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

DST="homeassistant-addon-dev"

if [ ! -d "$DST" ]; then
  echo "error: expected $DST/ to exist" >&2
  exit 1
fi

# Restore every tracked file under the dev dir from the index.
git checkout -- "$DST"

# Sweep files that copy-stable.sh introduced but the dev dir's git
# tree does not own (e.g. `start.py`, `CHANGELOG.md`). Untracked
# files in *subdirectories* the user may have added manually are left
# alone — only top-level files the stable dir is known to ship and
# the dev dir isn't are removed.
for f in start.py CHANGELOG.md .stable-flavor; do
  if [ -f "$DST/$f" ] && ! git ls-files --error-unmatch "$DST/$f" \
       >/dev/null 2>&1; then
    rm -f "$DST/$f"
  fi
done

echo "✔ Restored $DST/ from the index."
