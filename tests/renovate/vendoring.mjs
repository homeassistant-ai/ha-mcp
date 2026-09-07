// Exercise the scanner's real post-upgrade executor, including tool installation
// and artifact collection. Everything writable is a disposable local Git repo;
// no GitHub token, platform API, or remote PR writer is used.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const engine = process.env.RENOVATE_TEST_ROOT;
assert.ok(engine, 'RENOVATE_TEST_ROOT must name the installed Renovate package');
const load = (path) => import(pathToFileURL(join(engine, 'dist', path)).href);
const { init: initLogger } = await load('logger/index.js');
await initLogger();
const { getConfig } = await load('config/defaults.js');
const { GlobalConfig } = await load('config/global.js');
const { applyPackageRules } = await load('util/package-rules/index.js');
const { initRepo, syncGit } = await load('util/git/index.js');
const { isDynamicInstall } = await load('util/exec/containerbase.js');
const { api: pythonVersioning } = await load('modules/versioning/python/index.js');
const { default: executePostUpgradeCommands } = await load(
  'workers/repository/update/branch/execute-post-upgrade-commands.js'
);
const { parse } = createRequire(join(engine, 'package.json'))('yaml');
const repository = JSON.parse(readFileSync('/source/renovate.json', 'utf8'));
const workflow = parse(readFileSync('/source/.github/workflows/renovate.yml', 'utf8'));
const scannerEnv = workflow.jobs.renovate.steps.find(
  (step) => step.name === 'Self-hosted Renovate'
).env;
const pinFile = 'src/ha_mcp/_vendor/requirements.txt';
const vendorDir = 'src/ha_mcp/_vendor/websockets';
const dependency = {
  depName: 'websockets', packageName: 'websockets', datasource: 'pypi',
  manager: 'custom.regex', packageFile: pinFile,
};
const matched = await applyPackageRules({ ...getConfig(), ...repository, ...dependency });
assert.ok(matched.postUpgradeTasks.commands.length, 'Vendored updates must regenerate source');
for (const other of [
  { ...dependency, packageFile: 'other/requirements.txt' },
  { ...dependency, depName: 'other', packageName: 'other' },
  { ...dependency, datasource: 'docker' },
  { ...dependency, manager: 'pip_requirements' },
]) {
  const config = await applyPackageRules({ ...getConfig(), ...repository, ...other });
  assert.equal(config.postUpgradeTasks.commands.length, 0, 'Unrelated pins must not run vendoring');
}
assert.equal(matched.minimumReleaseAge, '7 days');
assert.deepEqual(matched.schedule, ['after 3pm on tuesday']);

// Resolving a Python range uses GitHub GraphQL, which requires authentication.
// Keep this fixture credential-free: install a published, compatible exact
// version through the real executor. The live scanner retains its range and
// authenticated lookup; this fixture does not test that external lookup.
const fixturePython = '3.13.7';
assert.ok(pythonVersioning.matches(fixturePython, matched.constraints.python));
const fixtureUpgrade = {
  ...matched, constraints: { ...matched.constraints, python: fixturePython },
};

const scratch = mkdtempSync(join(tmpdir(), 'renovate-vendoring-'));
const seed = join(scratch, 'seed');
const localDir = join(scratch, 'checkout');
mkdirSync(join(seed, vendorDir), { recursive: true });
mkdirSync(join(seed, 'scripts'));
mkdirSync(localDir);
copyFileSync('/source/scripts/vendor_websockets.py', join(seed, 'scripts/vendor_websockets.py'));
writeFileSync(join(seed, pinFile), 'websockets==17.0.1\n');
writeFileSync(join(seed, vendorDir, 'obsolete.py'), '# Must disappear when the tree is replaced.\n');
writeFileSync(join(seed, vendorDir, 'VENDORED'), 'websockets==17.0.1\n');
const git = (cwd, ...args) => execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' });
git(seed, 'init', '-b', 'master');
git(seed, 'add', '.');
git(seed, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture');

const allowedCommands = JSON.parse(scannerEnv.RENOVATE_ALLOWED_COMMANDS);
GlobalConfig.set({
  ...getConfig(), localDir, baseDir: scratch, cacheDir: join(scratch, 'cache'),
  containerbaseDir: join(scratch, 'containerbase'), binarySource: 'install',
  allowedCommands,
  allowShellExecutorForPostUpgradeCommands:
    scannerEnv.RENOVATE_ALLOW_SHELL_EXECUTOR_FOR_POST_UPGRADE_COMMANDS === 'true',
});
assert.ok(isDynamicInstall([{ toolName: 'python', constraint: matched.constraints.python }]),
  'This fixture must exercise containerbase tool installation, not a preinstalled Python');
await initRepo({ url: seed, defaultBranch: 'master', currentBranch: 'master', fullClone: true });
await syncGit(); // Clone only the local seed before writing the changed pin.
writeFileSync(join(localDir, 'unrelated.txt'), 'Never include this in the bot commit.\n');

const branch = (contents) => ({
  ...matched, branchName: 'renovate/websockets-17.x', baseBranch: 'master',
  upgrades: [{ ...fixtureUpgrade, currentValue: '17.0.1', newValue: '17.1' }],
  updatedPackageFiles: [{ type: 'addition', path: pinFile, contents }],
  updatedArtifacts: [], artifactErrors: [],
});

// A missing allowlist must report an artifact error and leave the tree stale.
GlobalConfig.set({ ...GlobalConfig.get(), allowedCommands: [] });
const denied = await executePostUpgradeCommands(branch('websockets==17.1\n'));
assert.ok(denied.artifactErrors.length);
assert.equal(readFileSync(join(localDir, vendorDir, 'VENDORED'), 'utf8'), 'websockets==17.0.1\n');
GlobalConfig.set({ ...GlobalConfig.get(), allowedCommands });

const result = await executePostUpgradeCommands(branch('websockets==17.1\n'));
assert.deepEqual(result.artifactErrors, [], 'Tool installation and regeneration must succeed');
const artifacts = new Map(result.updatedArtifacts.map((file) => [file.path, file]));
for (const name of ['VENDORED', 'LICENSE', 'MANIFEST.sha256', '__init__.py', 'version.py', 'asyncio/client.py']) {
  const artifact = artifacts.get(`${vendorDir}/${name}`);
  assert.equal(artifact?.type, 'addition', `${name} must be committed`);
  assert.equal(String(artifact.contents), readFileSync(join(localDir, vendorDir, name), 'utf8'));
}
assert.equal(artifacts.get(`${vendorDir}/obsolete.py`)?.type, 'deletion');
assert.ok([...artifacts.keys()].every((path) => path.startsWith(`${vendorDir}/`)),
  'Only vendored outputs belong in the generated artifacts');
assert.match(readFileSync(join(localDir, vendorDir, 'version.py'), 'utf8'), /tag = version = commit = ["']17\.1["']/);
assert.match(readFileSync(join(localDir, vendorDir, 'VENDORED'), 'utf8'), /^websockets==17\.1\n/);
const manifest = readFileSync(join(localDir, vendorDir, 'MANIFEST.sha256'), 'utf8').trim().split('\n');
for (const line of manifest) {
  const [digest, path] = line.split('  ');
  assert.equal(createHash('sha256').update(readFileSync(join(localDir, vendorDir, path))).digest('hex'), digest);
}
// A failed regeneration must stay visible, not silently accept a pin-only bump.
const failed = await executePostUpgradeCommands(branch('not-a-valid-pin\n'));
assert.ok(failed.artifactErrors.length, 'A failed generator must report an artifact error');
assert.match(readFileSync(join(localDir, vendorDir, 'VENDORED'), 'utf8'), /^websockets==17\.1\n/);
console.log('Pinned Renovate executor regenerated websockets source, license and manifest; collected additions/deletions; rejected unauthorized commands and surfaced generator failures.');
