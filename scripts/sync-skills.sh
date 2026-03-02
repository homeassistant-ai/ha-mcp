#!/usr/bin/env bash
# Sync bundled skills from the vendor/skills submodule into the package
# resource directory. Run after updating the submodule:
#
#   git submodule update --remote vendor/skills
#   bash scripts/sync-skills.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/vendor/skills/skills"
DST="$REPO_ROOT/src/ha_mcp/resources/skills"

if [ ! -d "$SRC" ]; then
    echo "Error: submodule not initialized. Run: git submodule update --init" >&2
    exit 1
fi

# Sync each skill directory
for skill_dir in "$SRC"/*/; do
    skill_name="$(basename "$skill_dir")"
    echo "Syncing skill: $skill_name"
    mkdir -p "$DST/$skill_name"
    rsync -a --delete "$skill_dir" "$DST/$skill_name/"
done

echo "Skills synced successfully."
