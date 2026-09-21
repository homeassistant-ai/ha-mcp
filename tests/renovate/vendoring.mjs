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
const allowedCommands = JSON.parse(scannerEnv.RENOVATE_ALLOWED_COMMANDS);
const pinFile = 'src/ha_mcp/_vendor/requirements.txt';
const git = (cwd, ...args) => execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' });

// Resolving a Python range uses GitHub GraphQL, which requires authentication.
// Keep this fixture credential-free: install a published, compatible exact
// version through the real executor. The live scanner retains its range and
// authenticated lookup; this fixture does not test that external lookup.
const fixturePython = '3.13.7';

async function exercise({ script, depName, pins, bumped, vendorDirs, extraFiles, check }) {
  const dependency = {
    depName, packageName: depName, datasource: 'pypi', manager: 'custom.regex', packageFile: pinFile,
  };
  const matched = await applyPackageRules({ ...getConfig(), ...repository, ...dependency });
  assert.deepEqual(matched.postUpgradeTasks.commands, [`python3 -I scripts/${script}`],
    `${depName} updates must regenerate source with ${script}`);
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
  assert.ok(pythonVersioning.matches(fixturePython, matched.constraints.python));
  const fixtureUpgrade = {
    ...matched, constraints: { ...matched.constraints, python: fixturePython },
  };

  const scratch = mkdtempSync(join(tmpdir(), 'renovate-vendoring-'));
  const seed = join(scratch, 'seed');
  const localDir = join(scratch, 'checkout');
  mkdirSync(join(seed, 'scripts'), { recursive: true });
  mkdirSync(join(seed, 'src/ha_mcp/_vendor'), { recursive: true });
  mkdirSync(localDir);
  copyFileSync(`/source/scripts/${script}`, join(seed, `scripts/${script}`));
  writeFileSync(join(seed, pinFile), pins);
  for (const dir of vendorDirs) {
    mkdirSync(join(seed, dir), { recursive: true });
    writeFileSync(join(seed, dir, 'obsolete.py'), '# Must disappear when the tree is replaced.\n');
    writeFileSync(join(seed, dir, 'VENDORED'), 'stale\n');
  }
  git(seed, 'init', '-b', 'master');
  git(seed, 'add', '.');
  git(seed, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture');

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
    ...matched, branchName: `renovate/${depName}`, baseBranch: 'master',
    upgrades: [{ ...fixtureUpgrade, ...bumped }],
    updatedPackageFiles: [{ type: 'addition', path: pinFile, contents }],
    updatedArtifacts: [], artifactErrors: [],
  });

  // A missing allowlist must report an artifact error and leave the tree stale.
  GlobalConfig.set({ ...GlobalConfig.get(), allowedCommands: [] });
  const denied = await executePostUpgradeCommands(branch(bumped.pins));
  assert.ok(denied.artifactErrors.length);
  for (const dir of vendorDirs) {
    assert.equal(readFileSync(join(localDir, dir, 'VENDORED'), 'utf8'), 'stale\n');
  }
  GlobalConfig.set({ ...GlobalConfig.get(), allowedCommands });

  const result = await executePostUpgradeCommands(branch(bumped.pins));
  assert.deepEqual(result.artifactErrors, [], 'Tool installation and regeneration must succeed');
  const artifacts = new Map(result.updatedArtifacts.map((file) => [file.path, file]));
  for (const dir of vendorDirs) {
    for (const name of ['VENDORED', 'LICENSE', 'MANIFEST.sha256', '__init__.py', ...(extraFiles[dir] || [])]) {
      const artifact = artifacts.get(`${dir}/${name}`);
      assert.equal(artifact?.type, 'addition', `${dir}/${name} must be committed`);
      assert.equal(String(artifact.contents), readFileSync(join(localDir, dir, name), 'utf8'));
    }
    assert.equal(artifacts.get(`${dir}/obsolete.py`)?.type, 'deletion');
    const manifest = readFileSync(join(localDir, dir, 'MANIFEST.sha256'), 'utf8').trim().split('\n');
    for (const line of manifest) {
      const [digest, path] = line.split('  ');
      assert.equal(createHash('sha256').update(readFileSync(join(localDir, dir, path))).digest('hex'), digest);
    }
  }
  assert.ok([...artifacts.keys()].every((path) => vendorDirs.some((dir) => path.startsWith(`${dir}/`))),
    'Only vendored outputs belong in the generated artifacts');
  check(localDir);

  // A failed regeneration must stay visible, not silently accept a pin-only bump.
  const failed = await executePostUpgradeCommands(branch('not-a-valid-pin\n'));
  assert.ok(failed.artifactErrors.length, 'A failed generator must report an artifact error');
  check(localDir);
}

await exercise({
  script: 'vendor_websockets.py',
  depName: 'websockets',
  pins: 'websockets==17.0.1\n',
  bumped: { currentValue: '17.0.1', newValue: '17.1', pins: 'websockets==17.1\n' },
  vendorDirs: ['src/ha_mcp/_vendor/websockets'],
  extraFiles: { 'src/ha_mcp/_vendor/websockets': ['version.py', 'asyncio/client.py'] },
  check: (localDir) => {
    const dir = join(localDir, 'src/ha_mcp/_vendor/websockets');
    assert.match(readFileSync(join(dir, 'version.py'), 'utf8'), /tag = version = commit = ["']17\.1["']/);
    assert.match(readFileSync(join(dir, 'VENDORED'), 'utf8'), /^websockets==17\.1\n/);
  },
});

await exercise({
  script: 'vendor_fastmcp.py',
  depName: 'fastmcp-slim',
  pins: 'fastmcp-slim==4.0.2\nmcp==2.2.0\nmcp-types==2.2.0\n',
  bumped: {
    currentValue: '4.0.2', newValue: '4.0.3', pins: 'fastmcp-slim==4.0.3\nmcp==2.2.0\nmcp-types==2.2.0\n',
  },
  vendorDirs: ['src/ha_mcp/_vendor/fastmcp', 'src/ha_mcp/_vendor/mcp', 'src/ha_mcp/_vendor/mcp_types'],
  extraFiles: {},
  check: (localDir) => {
    const vendor = join(localDir, 'src/ha_mcp/_vendor');
    assert.match(readFileSync(join(vendor, 'fastmcp/VENDORED'), 'utf8'), /^fastmcp-slim==4\.0\.3\n/);
    assert.match(readFileSync(join(vendor, 'fastmcp/__init__.py'), 'utf8'), /__version__ = "4\.0\.3"/);
    assert.doesNotMatch(readFileSync(join(vendor, 'mcp/__init__.py'), 'utf8'), /^\s*from mcp_types\b/m);
  },
});

console.log('Pinned Renovate executor regenerated the websockets and FastMCP/MCP SDK trees, licenses and manifests; collected additions/deletions; rejected unauthorized commands and surfaced generator failures.');
