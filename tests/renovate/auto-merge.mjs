// Exercise the actual approval guard and github-script body without credentials.
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
const workflow = parse(readFileSync('/source/.github/workflows/renovate-auto-merge.yml', 'utf8'));
const job = workflow.jobs.approve;
const expression = new Parser(new Lexer(job.if).lex().tokens, ['github'], []).parse();
const bot = 'ha-mcp-renovate[bot]';
const pr = {
  number: 123, state: 'open', draft: false, user: { login: bot },
  head: { ref: 'renovate/websockets-17.x', sha: 'a'.repeat(40), repo: { full_name: 'homeassistant-ai/ha-mcp' } },
  base: { ref: 'master' },
  auto_merge: { enabled_by: { login: bot }, merge_method: 'squash' },
};
const eventContext = {
  actor: bot, repository: 'homeassistant-ai/ha-mcp', event: { pull_request: pr },
};
const guardCases = [
  ['Renovate event', () => {}, true],
  ['human push', (g) => { g.actor = 'maintainer'; }, false],
  ['other bot', (g) => { g.actor = 'dependabot[bot]'; }, false],
  ['wrong author', (g) => { g.event.pull_request.user.login = 'maintainer'; }, false],
  ['fork', (g) => { g.event.pull_request.head.repo.full_name = 'fork/ha-mcp'; }, false],
  ['deleted repo', (g) => { g.event.pull_request.head.repo = null; }, false],
  ['wrong base', (g) => { g.event.pull_request.base.ref = 'stable'; }, false],
  ['closed', (g) => { g.event.pull_request.state = 'closed'; }, false],
  ['wrong branch', (g) => { g.event.pull_request.head.ref = 'feature/example'; }, false],
];
for (const [name, mutate, expected] of guardCases) {
  const github = structuredClone(eventContext);
  mutate(github);
  const context = JSON.parse(JSON.stringify({ github }), data.reviver);
  assert.equal(truthy(new Evaluator(expression, context).evaluate()), expected, name);
}

// Stub only GitHub's network boundary; execute the workflow's production body.
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const approve = new AsyncFunction('github', 'context', 'core', job.steps[0].with.script);
const approval = {
  user: { login: 'ghhamcp' }, commit_id: 'a'.repeat(40), state: 'APPROVED',
};
const scriptCases = [
  ['current eligible head', () => {}, true],
  ['closed after event', (s) => { s.live.state = 'closed'; }, false],
  ['draft after event', (s) => { s.live.draft = true; }, false],
  ['wrong live author', (s) => { s.live.user.login = 'maintainer'; }, false],
  ['fork after event', (s) => { s.live.head.repo.full_name = 'fork/ha-mcp'; }, false],
  ['deleted live repo', (s) => { s.live.head.repo = null; }, false],
  ['changed head', (s) => { s.live.head.sha = 'b'.repeat(40); }, false],
  ['changed base', (s) => { s.live.base.ref = 'stable'; }, false],
  ['changed branch', (s) => { s.live.head.ref = 'feature/example'; }, false],
  ['disabled auto-merge', (s) => { s.live.auto_merge = null; }, false],
  ['human-enabled auto-merge', (s) => { s.live.auto_merge.enabled_by.login = 'maintainer'; }, false],
  ['missing auto-merge actor', (s) => { s.live.auto_merge.enabled_by = null; }, false],
  ['wrong merge strategy', (s) => { s.live.auto_merge.merge_method = 'merge'; }, false],
  ['already approved', (s) => { s.reviews = [approval]; }, false],
  ['old approval', (s) => { s.reviews = [{ ...approval, commit_id: 'b'.repeat(40) }]; }, true],
  ['dismissed approval', (s) => { s.reviews = [{ ...approval, state: 'DISMISSED' }]; }, true],
  ['other reviewer', (s) => { s.reviews = [{ ...approval, user: { login: 'other' } }]; }, true],
  ['wrong approval token identity', (s) => { s.account = 'other'; }, false, /ghhamcp maintainer token/],
  ['API error', (s) => { s.apiError = true; }, false, /API unavailable/],
];
for (const [name, mutate, expected, error] of scriptCases) {
  const state = { live: structuredClone(pr), account: 'ghhamcp', reviews: [], apiError: false };
  mutate(state);
  const writes = [];
  const coordinates = { owner: 'homeassistant-ai', repo: 'ha-mcp', pull_number: 123 };
  const listReviews = () => { throw new Error('Use paginated reviews'); };
  const github = {
    rest: {
      pulls: {
        get: async (args) => {
          assert.deepEqual(args, coordinates);
          if (state.apiError) throw new Error('API unavailable');
          return { data: state.live };
        },
        listReviews,
        createReview: async (args) => { writes.push(args); },
      },
      users: { getAuthenticated: async () => ({ data: { login: state.account } }) },
    },
    paginate: async (method, args) => {
      assert.equal(method, listReviews);
      assert.deepEqual(args, { ...coordinates, per_page: 100 });
      return state.reviews;
    },
  };
  const run = () => approve(github, {
    repo: { owner: 'homeassistant-ai', repo: 'ha-mcp' },
    payload: { pull_request: structuredClone(pr) },
  }, { info: () => {} });
  if (error) await assert.rejects(run, error, name);
  else await run();
  assert.deepEqual(writes, expected ? [{
    ...coordinates, event: 'APPROVE', commit_id: 'a'.repeat(40),
    body: 'Automated approval: Renovate enabled auto-merge for this update; required checks remain authoritative.',
  }] : [], name);
}
console.log(`Renovate approval passed ${guardCases.length} event and ${scriptCases.length} live-state cases.`);
