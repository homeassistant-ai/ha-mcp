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

# Collect skill directories (nullglob prevents literal glob on empty match)
shopt -s nullglob
skill_dirs=("$SRC"/*/)
shopt -u nullglob

if [ ${#skill_dirs[@]} -eq 0 ]; then
    echo "Error: No skill directories found in $SRC. Is the submodule initialized?" >&2
    exit 1
fi

# Remove skills that no longer exist upstream
if [ -d "$DST" ]; then
    shopt -s nullglob
    existing_dirs=("$DST"/*/)
    shopt -u nullglob
    for existing_dir in "${existing_dirs[@]}"; do
        skill_name="$(basename "$existing_dir")"
        if [ ! -d "$SRC/$skill_name" ]; then
            echo "Removing deleted skill: $skill_name"
            rm -rf "$existing_dir"
        fi
    done
fi

# Sync each skill directory
for skill_dir in "${skill_dirs[@]}"; do
    skill_name="$(basename "$skill_dir")"
    echo "Syncing skill: $skill_name"
    mkdir -p "$DST/$skill_name"
    rsync -a --delete "$skill_dir" "$DST/$skill_name/"
done

echo "Skills synced successfully."
