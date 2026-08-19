#!/usr/bin/env bash
# Print the shared HAOS E2E image cache-key suffix.
#
# Both the master image builder and every test lane call this helper. Keep all
# baked-image inputs here so a workflow edit cannot silently warm and restore
# different cache keys.
set -euo pipefail

tree_hash=$(
  git ls-tree -r HEAD \
    tests/haos_image_build \
    tests/initial_test_state \
    custom_components/ha_mcp_tools \
    homeassistant-addon-webhook-proxy \
    | sha256sum
)
tree_hash=${tree_hash%% *}
printf '%.16s\n' "$tree_hash"
