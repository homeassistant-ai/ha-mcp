// Run inside the scanner's pinned image: use Renovate's real policy functions,
// not a second implementation of matching, scheduling, age, or rate limits.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const engine = process.env.RENOVATE_TEST_ROOT;
assert.ok(engine, 'RENOVATE_TEST_ROOT must name the installed Renovate package');
const load = (path) => import(pathToFileURL(join(engine, 'dist', path)).href);
const { getConfig } = await load('config/defaults.js');
const { applyPackageRules } = await load('util/package-rules/index.js');
const { checkMinimumReleaseAge } = await load('util/minimum-release-age.js');
const { isScheduledNow } = await load('workers/repository/update/branch/schedule.js');
const { calcLimit } = await load('workers/global/limits.js');
const repository = JSON.parse(readFileSync('/source/renovate.json', 'utf8'));
const NativeDate = Date;
let now = NativeDate.parse('2026-09-06T12:00:00Z'); // Sunday, outside the window.
globalThis.Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
};

try {
  const age = (days) => new Date(now - days * 86400000).toISOString();
  for (const [depName, datasource] of [
    ['ghcr.io/home-assistant/home-assistant', 'docker'],
    ['home-assistant/supervisor', 'custom.ha-supervisor-stable'],
    ['home-assistant/operating-system', 'github-releases'],
  ]) {
    const config = await applyPackageRules({
      ...getConfig(), ...repository, depName, packageName: depName,
      datasource, manager: 'custom.regex', packageFile: 'tests/haos_image_build/build_image.py',
    });
    assert.equal(isScheduledNow(config), true, `${depName}: Sunday must be eligible`);
    assert.equal(checkMinimumReleaseAge(config, age(1 / 24)).isPending, false,
      `${depName}: a one-hour-old release must be eligible`);
    for (const limit of ['prHourlyLimit', 'prConcurrentLimit', 'branchConcurrentLimit']) {
      assert.equal(calcLimit([config], limit), 0, `${depName}: ${limit}`);
    }
  }

  const ordinary = await applyPackageRules({
    ...getConfig(), ...repository, depName: 'astral-sh/uv', packageName: 'astral-sh/uv',
    datasource: 'github-releases', manager: 'custom.regex',
  });
  assert.equal(isScheduledNow(ordinary), false, 'Ordinary Sunday updates must wait');
  assert.equal(ordinary.updateNotScheduled, false, 'Existing ordinary branches must wait too');
  assert.equal(checkMinimumReleaseAge(ordinary, age(1)).isPending, true);
  assert.equal(checkMinimumReleaseAge(ordinary, age(8)).isPending, false);

  now = NativeDate.parse('2026-09-08T16:00:00Z'); // Tuesday, inside the window.
  assert.equal(isScheduledNow(ordinary), true, 'Mature ordinary updates can run on Tuesday');
  assert.equal(checkMinimumReleaseAge(ordinary, age(1)).isPending, true,
    'Tuesday must not bypass the seven-day age gate');
  assert.equal(checkMinimumReleaseAge(ordinary, age(8)).isPending, false);
  for (const updateType of ['minor', 'patch', 'digest', 'major', 'pin', 'pinDigest', 'replacement']) {
    const config = await applyPackageRules({
      ...getConfig(), ...repository, depName: 'astral-sh/uv', packageName: 'astral-sh/uv',
      datasource: 'github-releases', manager: 'custom.regex', updateType,
    });
    assert.equal(config.automerge, ['minor', 'patch', 'digest'].includes(updateType),
      `${updateType}: ordinary automerge eligibility`);
    assert.equal(config.automergeType, 'pr');
    assert.equal(config.automergeStrategy, 'squash');
    assert.equal(config.platformAutomerge, true);
    assert.equal(checkMinimumReleaseAge(config, age(1)).isPending, true,
      'Automerge must not bypass ordinary age gates');
  }
  // Renovate merges this object into each alert-driven update; keep native
  // ungrouped security fixes eligible even if the fix requires a major bump.
  const security = { ...getConfig().vulnerabilityAlerts, ...repository.vulnerabilityAlerts };
  assert.equal(security.automerge, true);
  assert.equal(security.groupName, null);
  assert.equal(security.minimumReleaseAge, null);
  console.log('Pinned Renovate engine passed HA/ordinary scheduling, age, limit, and automerge fixtures.');
} finally {
  globalThis.Date = NativeDate;
}
