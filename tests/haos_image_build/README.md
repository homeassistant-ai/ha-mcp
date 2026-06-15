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
shared Actions cache used by the E2E lanes — `haos-e2e-tests.yml` and
`haos-e2e-inaddon-tests.yml` restore the qcow2 from that cache and fall back
to a local build on a miss.

## Version pinning

`HAOS_VERSION` in `build_image.py` is the single source of truth. Renovate
watches the `home-assistant/operating-system` releases via the annotation
comment above the constant and opens a bump PR when HAOS releases. The
image-build workflow runs on that PR (uploading the new qcow2 as a workflow
artifact so reviewers can sanity-check). Merging the PR triggers the
master-only cache-prime; the E2E lanes then restore the new image from the
shared cache automatically.
