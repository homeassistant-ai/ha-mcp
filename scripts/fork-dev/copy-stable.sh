#!/usr/bin/env bash
# Copy the stable add-on flavour over the dev add-on directory so
# `homeassistant-addon-dev/` builds and runs as the stable add-on.
#
# Why this exists: the fork-dev add-on (`homeassistant-addon-dev/`) is
# the only deployment path some maintainers have available for local
# Home Assistant testing — they can't install the real stable add-on
# alongside it. This script overwrites the dev directory with the
# stable directory's contents so a single `git push addon-repo`
# delivers a stable-flavored build under the fork-dev slug.
#
# What it does:
#   - Replaces every shared file (config.yaml, DOCS.md, Dockerfile,
#     start.py, README.md, translations/, CHANGELOG.md) in
#     `homeassistant-addon-dev/` with the stable copy.
#   - Patches the copied `config.yaml` to keep the dev add-on's
#     identity so Home Assistant Supervisor doesn't collide with the
#     real stable add-on:
#       * name  → "Home Assistant MCP Server (Fork-Dev Stable Test)"
#       * slug  → "ha_mcp_dev"
#       * version → "<stable>+stable-test"
#       * removes the prebuilt `image:` line so Supervisor builds the
#         image locally from the source the fork-dev branch ships
#         (the real stable image is pinned to upstream's container
#         registry and can't be reused here)
#       * adds the dev ingress lines back so the web UI keeps working
#   - Patches the copied `Dockerfile` LABEL so the dev tag appears in
#     `docker inspect` output.
#   - Writes `homeassistant-addon-dev/.stable-flavor` as a marker so
#     `restore-dev.sh` (and the maintainer) can see which flavour is
#     currently staged.
#
# Run from the repo root. To revert, run `scripts/fork-dev/restore-dev.sh`.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

SRC="homeassistant-addon"
DST="homeassistant-addon-dev"

if [ ! -d "$SRC" ] || [ ! -d "$DST" ]; then
  echo "error: expected both $SRC/ and $DST/ to exist" >&2
  exit 1
fi

if [ -f "$DST/.stable-flavor" ]; then
  echo "warn: $DST already contains a stable-flavor marker; overwriting" >&2
fi

# 1. Wipe the dev dir's tracked files (preserve .stable-flavor for
#    the warn-on-re-run case above; restore-dev.sh will clear it).
#    Use git to enumerate so we don't accidentally delete untracked
#    local edits the user is mid-flight on.
git ls-files "$DST" | while read -r f; do
  rm -f "$f"
done
rmdir "$DST/translations" 2>/dev/null || true

# 2. Copy stable -> dev, preserving the relative tree.
cp -R "$SRC/." "$DST/"

# 3. Patch config.yaml. Match the user's existing fork-dev identity
#    (slug=ha_mcp_dev, name suffix, no image line, ingress on).
CONFIG="$DST/config.yaml"
python3 - "$CONFIG" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
src = path.read_text()

# Identity rewrite (keep the dev slug so HA Supervisor doesn't see
# this as the real stable add-on).
src = re.sub(
    r'^name:\s*"[^"]*"',
    'name: "Home Assistant MCP Server (Fork-Dev Stable Test)"',
    src, count=1, flags=re.M,
)
src = re.sub(
    r'^description:\s*"[^"]*"',
    'description: "Stable-flavor of fork-dev — for testing the stable add-on UX from the fork-dev branch."',
    src, count=1, flags=re.M,
)
src = re.sub(r'^slug:\s*"[^"]*"', 'slug: "ha_mcp_dev"', src, count=1, flags=re.M)
# Tag the version so a Supervisor info screen makes the flavor obvious.
src = re.sub(
    r'^(version:\s*")([^"]+)(")',
    r'\g<1>\g<2>+stable-test\g<3>',
    src, count=1, flags=re.M,
)

# Drop the prebuilt-image line; Supervisor builds locally instead.
# The dev workflow does not have a stable image registry to point at.
src = re.sub(
    r'^# Use pre-built Docker images\n',
    '',
    src, count=1, flags=re.M,
)
src = re.sub(r'^image:\s*"[^"]*"\n', '', src, count=1, flags=re.M)

# Re-insert ingress + experimental stage (dev has these; stable does
# not). Put them right after `homeassistant_api: true` to mirror the
# layout the user already runs.
if 'ingress: true' not in src:
    src = re.sub(
        r'(^homeassistant_api:\s*true\n)',
        r'\1ingress: true\ningress_port: 9583\ningress_stream: true\n',
        src, count=1, flags=re.M,
    )
if 'stage: experimental' not in src:
    src = re.sub(
        r'(^url:\s*"[^"]+"\n)',
        r'\1stage: experimental\n',
        src, count=1, flags=re.M,
    )

path.write_text(src)
PY

# 4. Patch Dockerfile LABEL so `docker inspect` and any HA UI that
#    surfaces it sees the dev tag, not the stable tag. Same regex
#    target either Dockerfile uses for the io.hass.name LABEL.
DF="$DST/Dockerfile"
if [ -f "$DF" ]; then
  sed -i \
    -e 's|io\.hass\.name="Home Assistant MCP Server"|io.hass.name="Home Assistant MCP Server (Fork-Dev Stable Test)"|' \
    -e 's|io\.hass\.description="AI assistant integration via Model Context Protocol"|io.hass.description="Stable-flavor of fork-dev — for testing the stable add-on UX from the fork-dev branch."|' \
    "$DF"
fi

# 5. Drop the marker so the maintainer (and restore-dev.sh) can tell
#    which flavour is currently staged.
echo "stable-flavor staged at $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "$DST/.stable-flavor"

cat <<EOF
✔ Staged stable flavor into $DST/.
  Next:
    1. Review the diff:  git diff --stat $DST
    2. Bump the version in $DST/config.yaml if needed (current value
       has '+stable-test' appended so HA recognises it as a new build).
    3. Commit + push to addon-repo so HA Supervisor pulls it.
  To revert:
    scripts/fork-dev/restore-dev.sh
EOF
