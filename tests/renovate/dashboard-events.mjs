// Evaluate the actual token-bearing job guard with GitHub's expression library.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const engineRequire = createRequire(join(process.env.RENOVATE_TEST_ROOT, 'package.json'));
const { parse } = engineRequire('yaml');
const { Lexer, Parser, Evaluator, data } = await import(pathToFileURL(
  '/tmp/expression-tests/node_modules/@actions/expressions/dist/index.js'
).href);
const { truthy } = await import(pathToFileURL(
  '/tmp/expression-tests/node_modules/@actions/expressions/dist/result.js'
).href);
const workflow = parse(readFileSync('/source/.github/workflows/renovate.yml', 'utf8'));
const expression = new Parser(new Lexer(workflow.jobs.renovate.if).lex().tokens, ['github'], []).parse();
const dashboard = {
  event_name: 'issues',
  event: {
    issue: { title: 'Dependency Dashboard', user: { login: 'ha-mcp-renovate[bot]' }, body: '- [x] request' },
    sender: { type: 'User' }, changes: { body: { from: '- [ ] request' } },
  },
};
const cases = [
  ['human checked request', () => {}, true],
  ['uppercase checked request', (g) => { g.event.issue.body = '- [X] request'; }, true],
  ['bot edit', (g) => { g.event.sender.type = 'Bot'; }, false],
  ['unrelated issue', (g) => { g.event.issue.title = 'Other issue'; }, false],
  ['spoofed dashboard author', (g) => { g.event.issue.user.login = 'someone-else'; }, false],
  ['unchanged body', (g) => { delete g.event.changes.body; }, false],
  ['unchecked body', (g) => { g.event.issue.body = '- [ ] request'; }, false],
  ['manual dispatch', (g) => { g.event_name = 'workflow_dispatch'; g.event = {}; }, true],
  ['hourly schedule', (g) => { g.event_name = 'schedule'; g.event = {}; }, true],
];
const pr = {
  event_name: 'pull_request_target',
  repository: 'homeassistant-ai/ha-mcp',
  event: {
    action: 'edited',
    pull_request: {
      state: 'open',
      user: { login: 'ha-mcp-renovate[bot]' },
      head: { ref: 'renovate/websockets-17.x', repo: { full_name: 'homeassistant-ai/ha-mcp' } },
      base: { ref: 'master' },
      body: '- [x] <!-- rebase-check -->If you want to rebase/retry this PR, check this box',
    },
    sender: { type: 'User' },
    changes: { body: { from: '- [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box' } },
  },
};
const prCases = [
  ['PR checkbox checked', () => {}, true],
  ['uppercase PR checkbox', (g) => { g.event.pull_request.body = '- [X] <!-- rebase-check -->retry'; }, true],
  ['bot PR edit', (g) => { g.event.sender.type = 'Bot'; }, false],
  ['missing sender', (g) => { delete g.event.sender; }, false],
  ['human-authored PR', (g) => { g.event.pull_request.user.login = 'someone-else'; }, false],
  ['fork PR', (g) => { g.event.pull_request.head.repo.full_name = 'someone-else/ha-mcp'; }, false],
  ['deleted head repository', (g) => { g.event.pull_request.head.repo = null; }, false],
  ['non-Renovate branch', (g) => { g.event.pull_request.head.ref = 'feature/example'; }, false],
  ['wrong base', (g) => { g.event.pull_request.base.ref = 'stable'; }, false],
  ['closed PR', (g) => { g.event.pull_request.state = 'closed'; }, false],
  ['PR title-only edit', (g) => { g.event.changes = { title: { from: 'old' } }; }, false],
  ['PR uncheck', (g) => {
    g.event.pull_request.body = '- [ ] <!-- rebase-check -->retry';
    g.event.changes.body.from = '- [x] <!-- rebase-check -->retry';
  }, false],
  ['already checked PR with unrelated body edit', (g) => {
    g.event.changes.body.from = '- [x] <!-- rebase-check -->retry';
  }, false],
  ['already checked uppercase PR', (g) => {
    g.event.changes.body.from = '- [X] <!-- rebase-check -->retry';
  }, false],
  ['unrelated PR checkbox', (g) => { g.event.pull_request.body = '- [x] tests pass'; }, false],
  ['empty PR body', (g) => { g.event.pull_request.body = null; }, false],
  ['PR synchronize', (g) => { g.event.action = 'synchronize'; }, false],
  ['ordinary pull_request event', (g) => { g.event_name = 'pull_request'; }, false],
];
// This fixture evaluates the actual workflow expression, not a rewritten guard.
// Trigger registration and trusted checkout inputs are covered by the unit test.
for (const [name, mutate, expected, fixture] of [
  ...cases.map((entry) => [...entry, dashboard]),
  ...prCases.map((entry) => [...entry, pr]),
]) {
  const github = structuredClone(fixture);
  mutate(github);
  const context = JSON.parse(JSON.stringify({ github }), data.reviver);
  // Logical expressions can short-circuit to null; a job guard uses truthiness,
  // not the string representation of the result.
  assert.equal(truthy(new Evaluator(expression, context).evaluate()), expected, name);
}
console.log(`GitHub expression evaluator passed ${cases.length + prCases.length} dashboard/PR-event cases.`);
