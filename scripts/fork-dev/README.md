# Fork-dev helpers

Scripts for swapping the `homeassistant-addon-dev/` directory between
its native dev flavor and a stable-mirroring "stable test" flavor —
useful for maintainers whose only HA test path is the fork-dev add-on
slot (they cannot install the real stable add-on alongside it).

| Script | What it does |
|---|---|
| `copy-stable.sh` | Overwrites `homeassistant-addon-dev/` with the contents of `homeassistant-addon/`, patching `config.yaml` + `Dockerfile` to keep the dev add-on's slug/name/version so HA Supervisor sees it as the fork-dev add-on (not a duplicate of real stable). Writes `homeassistant-addon-dev/.stable-flavor` as a marker. |
| `restore-dev.sh` | Reverts every tracked file under `homeassistant-addon-dev/` to the index (`git checkout --`) and sweeps the marker + the known stable-only files (`start.py`, `CHANGELOG.md`) that `copy-stable.sh` introduces. Idempotent. |

## Typical workflow

```bash
# Stage the stable flavor into the dev addon dir
scripts/fork-dev/copy-stable.sh

# Sanity-check the patches
git diff --stat homeassistant-addon-dev/

# Push to the addon-repo branch for HA Supervisor to pick up
# (see your FORK-DEV.md for the addon-repo push procedure).

# When done, revert
scripts/fork-dev/restore-dev.sh
```

## Notes

- `copy-stable.sh` uses `git ls-files` to enumerate what to wipe in
  `homeassistant-addon-dev/` before copying, so untracked local edits
  are preserved.
- The patched `config.yaml` ends with `version: "<stable>+stable-test"`
  so HA Supervisor recognizes it as a new build distinct from any
  prior dev version cached on the device.
- The dev flavor is the default; `copy-stable.sh` is opt-in and
  reversible.
