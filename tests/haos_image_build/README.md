# HAOS Test Image Build

Builds the pre-baked HAOS qcow2 used by the HAOS E2E test tier (#1281).
The image bundles a configured HAOS install with the ha-mcp addon repository
registered, a v1 set of addons installed (Frigate, ESPHome, Node-RED,
Mosquitto, Zigbee2MQTT), and HACS bootstrapped.

## Local build

Requirements:
- Linux host with `/dev/kvm` accessible
- `qemu-system-x86`, `qemu-utils`, `ovmf`, `xz-utils`, `curl`
- ~10 GB free disk in the work directory

```bash
python3 tests/haos_image_build/build_image.py --verbose \
  --work-dir /tmp/haos-build \
  --output haos-test-image.qcow2.xz
```

First boot pulls the HAOS release (~530 MB compressed), expands the data
partition, then runs onboarding and addon installs. Total wall time on a
4-vCPU runner: ~15–25 minutes depending on addon Docker pulls.

## CI build

`build-haos-test-image.yml` runs the same script on `ubuntu-22.04`. On every
run it uploads the qcow2 as a workflow artifact (reviewer sanity-check). On
`master` (push / weekly cron / manual dispatch) it additionally primes the
shared Actions cache used by all six E2E lanes. They all live in
`haos-e2e-tests.yml` as the jobs `haos-e2e`, `haos-e2e-inaddon`,
`haos-e2e-inaddon-no-tools`, `haos-e2e-embedded`,
`haos-e2e-embedded-no-tools`, and `haos-e2e-stdio`; each restores the qcow2
from that cache and falls back to a local build on a miss.

## Version pinning

`build_image.py` has three Renovate-managed stable inputs:

- `STABLE_HAOS_VERSION`: the stable operating-system release.
- `STABLE_SUPERVISOR_VERSION`: the promoted Supervisor version from
  `https://version.home-assistant.io/stable.json`, used as a minimum.
- `STABLE_CORE_VERSION`: the exact Core version, updated together with the
  container E2E pins.

All three dependencies bypass Renovate's ordinary seven-day age gate and
Tuesday window. The scanner runs hourly. A builder-input change invalidates
the stable image cache; the PR's HAOS E2E lanes build on a miss. Merging the PR
also triggers the master image-cache prime. Supervisor can still self-update
past its recorded minimum within the selected channel.

Beta lanes independently read `beta.json`: `hassos.ova`, `supervisor`, and
`homeassistant.qemux86-64`. They set `HAOS_BUILD_OS_VERSION`,
`HAOS_BUILD_SUPERVISOR_CHANNEL=beta`, `HAOS_BUILD_SUPERVISOR_MIN_VERSION`, and
`HAOS_BUILD_CORE_VERSION`. These environment overrides also remain available
for explicit local builds. OS prerelease versions such as `18.2.rc1` select
the corresponding release qcow2 instead of the stable OS pin.

The shared beta cache key includes all three resolved versions plus repository
bake inputs. Only the in-app beta lane writes that cache; the embedded beta
lane restores it or builds on a miss. Automatic beta runs skip only when all
three channel versions equal stable, so an OS-only beta release still runs.
Manual dispatch always runs, and the beta canary checks the booted VM's OS,
Supervisor channel/minimum, and exact Core version.
