import test from "node:test";
import assert from "node:assert/strict";
import {
  mkdtempSync,
  writeFileSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import {
  command,
  checksReady,
  decide,
  digest,
  renderState,
  resultSchema,
  stateFrom,
  validateChanges,
  validateResult,
} from "../../.github/slash-agent/core.mjs";
import {
  API,
  collect,
  eventTarget,
  sessionRoot,
  snapshotDifferences,
} from "../../.github/slash-agent/github.mjs";
import { main, prepare, prompt } from "../../.github/slash-agent/main.mjs";
import { publish } from "../../.github/slash-agent/publish.mjs";
import { packageWork } from "../../.github/slash-agent/worker.mjs";

const A = "a".repeat(40),
  B = "b".repeat(40),
  APP = "ha-mcp";
const user = { login: "maintainer", type: "User" },
  bot = { login: `${APP}[bot]`, type: "Bot" };
const response = () => ({
  title: "fix: preserve automation scope",
  summary: "Fixed the accepted scope.",
  tests: "node --test passed",
  memory: "Only automation enable/disable was approved.",
  outcome: "changed",
  responses: [],
});
const artifact = () => ({
  result: response(),
  threadId: "11111111-1111-1111-1111-111111111111",
  changes: [
    {
      path: "src/example.py",
      mode: "100644",
      content: Buffer.from("fixed\n").toString("base64"),
    },
  ],
});

class FakeAPI {
  constructor() {
    this.repository = "test/repo";
    this.root = "repos/test/repo";
    this.issue = {
      number: 9,
      title: "Automation scope",
      body: "Implement automation enable/disable",
      state: "open",
      locked: false,
      user,
    };
    this.command = {
      id: 1,
      body: "/astra implement the agreed scope",
      user,
      updated_at: "2026-09-15T10:00:00Z",
      issue_url: `${this.root}/issues/9`,
    };
    this.comments = [this.command];
    this.prComments = [];
    this.reviewThreads = [];
    this.reviews = [];
    this.checks = [];
    this.statuses = [];
    this.branches = { master: A };
    this.pr = null;
    this.calls = [];
    this.roles = { maintainer: "maintain", contributor: "write" };
    this.runs = {};
  }
  request(path) {
    assert.equal(path, this.root);
    return { default_branch: "master" };
  }
  get(path) {
    if (path === "issues/9") return structuredClone(this.issue);
    if (path === "issues/10")
      return { ...this.issue, number: 10, pull_request: {} };
    if (path === "pulls/10") return structuredClone(this.pr);
    if (path.startsWith("branches/")) {
      const branch = decodeURIComponent(path.slice(9));
      if (!this.branches[branch])
        throw Object.assign(Error("missing"), { status: 404 });
      return {
        protected: !!this.protected,
        commit: { sha: this.branches[branch] },
      };
    }
    if (path.startsWith("git/ref/heads/")) {
      const sha = this.branches[decodeURIComponent(path.slice(14))];
      if (!sha) throw Object.assign(Error("missing"), { status: 404 });
      return { object: { sha } };
    }
    if (path.startsWith("git/commits/")) return { tree: { sha: A } };
    if (path.startsWith("rules/branches/"))
      return [
        {
          type: "required_status_checks",
          parameters: {
            required_status_checks: [
              { context: "Unit Tests", integration_id: 15368 },
            ],
          },
        },
      ];
    if (path.startsWith("issues/comments/")) {
      const c = [...this.comments, ...this.prComments].find(
        (c) => c.id === Number(path.split("/").at(-1)),
      );
      if (!c) throw Object.assign(Error("missing"), { status: 404 });
      return structuredClone(c);
    }
    if (path.startsWith("actions/runs/"))
      return this.runs[path.split("/").at(-1)];
    throw Error(`Unexpected read ${path}`);
  }
  optional(path) {
    try {
      return this.get(path);
    } catch (e) {
      if (e.status === 404) return null;
      throw e;
    }
  }
  role(login) {
    return this.roles[login] ?? "none";
  }
  edits(comments) {
    return structuredClone(
      comments.map((c) => ({
        editingVerified: true,
        edited_at: null,
        editor: null,
        ...c,
      })),
    );
  }
  pages(path) {
    if (path === "issues/9/comments") return structuredClone(this.comments);
    if (path === "issues/10/comments") return structuredClone(this.prComments);
    if (path === "pulls/10/reviews") return structuredClone(this.reviews);
    if (path.includes("check-runs")) return structuredClone(this.checks);
    if (path.endsWith("/statuses")) return structuredClone(this.statuses);
    if (path.startsWith("pulls?"))
      return this.pr ? [structuredClone(this.pr)] : [];
    if (path.endsWith("/pulls"))
      return this.pr ? [structuredClone(this.pr)] : [];
    throw Error(`Unexpected list ${path}`);
  }
  threads() {
    return structuredClone(
      this.reviewThreads.map((t) => ({
        ...t,
        comments: this.edits(t.comments),
      })),
    );
  }
  write(path, data, method = "POST") {
    this.calls.push({ path, data: structuredClone(data), method });
    if (path === "issues/9/comments") {
      const c = {
        id: 100,
        user: bot,
        editingVerified: true,
        edited_at: null,
        editor: null,
        ...data,
      };
      this.comments.push(c);
      return c;
    }
    if (path === "issues/comments/100") {
      Object.assign(
        this.comments.find((c) => c.id === 100),
        data,
        {
          editingVerified: true,
          editor: bot,
          edited_at: "2026-09-15T12:00:00Z",
        },
      );
      return {};
    }
    if (path === "issues/10/comments") {
      const comment = { id: 1000 + this.prComments.length, user: bot, ...data };
      this.prComments.push(comment);
      return comment;
    }
    if (/^(issues|pulls)\/comments\/\d+$/.test(path)) {
      const id = Number(path.split("/").at(-1));
      const comment = [
        ...this.prComments,
        ...this.reviewThreads.flatMap((t) => t.comments),
      ].find((c) => c.id === id);
      assert.ok(comment);
      Object.assign(comment, data, {
        editor: bot,
        edited_at: "2026-09-15T14:00:00Z",
        updated_at: "2026-09-15T14:00:00Z",
        editingVerified: true,
      });
      return comment;
    }
    if (path === "git/blobs" || path === "git/trees")
      return { sha: digest(data).slice(0, 40) };
    if (path === "git/commits") {
      assert.deepEqual(data.parents, [
        this.pr?.head.sha ?? this.branches["agents/issue-9"] ?? A,
      ]);
      return { sha: B };
    }
    if (path === "git/refs") {
      this.branches[data.ref.slice(11)] = data.sha;
      return {};
    }
    if (path.startsWith("git/refs/heads/")) {
      assert.equal(data.force, false);
      this.branches[decodeURIComponent(path.slice(15))] = data.sha;
      this.pr.head.sha = data.sha;
      return {};
    }
    if (path === "pulls") {
      assert.equal(data.draft, true);
      this.pr = {
        number: 10,
        node_id: "PR_10",
        user: bot,
        state: "open",
        draft: true,
        mergeable: true,
        ...data,
        base: { ref: data.base },
        head: {
          ref: data.head,
          sha: this.branches[data.head],
          repo: { full_name: this.repository },
        },
      };
      return this.pr;
    }
    if (path === "pulls/10") {
      Object.assign(this.pr, data);
      return this.pr;
    }
    if (path.includes("/replies")) {
      const reply = {
        id: 901,
        user: bot,
        body: data.body,
      };
      this.reviewThreads[0].comments.push(reply);
      return reply;
    }
    throw Error(`Unexpected write ${path}`);
  }
  graphql(query, variables) {
    this.calls.push({ query, variables });
    if (query.includes("markPullRequestReadyForReview")) this.pr.draft = false;
    else if (query.includes("resolveReviewThread"))
      this.reviewThreads.find((t) => t.id === variables.id).isResolved = true;
    else throw Error("Unexpected GraphQL mutation");
    return {};
  }
}

const initial = (api) =>
  prepare(api, { number: 9, commandId: 1, automatic: false }, APP);
const start = (api) =>
  publish(api, initial(api), artifact(), APP, { runId: "42" });
const green = (api) => {
  api.checks = [
    {
      id: 3,
      name: "Unit Tests",
      app: { id: 15368 },
      status: "completed",
      conclusion: "success",
    },
  ];
};

const readySession = () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  publish(api, prepare(api, { number: 10, automatic: true }, APP), null, APP, { runId: "43" });
  return api;
};

test("healthy pushes wait or update readiness without buying a worker turn", () => {
  for (const pending of [false, true]) {
    const api = readySession();
    const rounds = stateFrom(api.edits(api.comments), APP).rounds;
    api.pr.head.sha = "c".repeat(40);
    api.branches[api.pr.head.ref] = api.pr.head.sha;
    if (pending) {
      api.checks[0].status = "in_progress";
      api.checks[0].conclusion = null;
    }
    const plan = prepare(api, { number: 10, automatic: true }, APP);
    if (pending) assert.equal(plan, null);
    else {
      assert.equal(plan.decision.mode, "ready");
      publish(api, plan, null, APP, { runId: "44" });
      assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
      assert.equal(stateFrom(api.edits(api.comments), APP).lastHead, api.pr.head.sha);
    }
    assert.equal(stateFrom(api.edits(api.comments), APP).rounds, rounds);
  }
});

test("ordinary maintainer comments and the Codex setup notice cannot buy a turn", () => {
  const api = readySession();
  api.prComments.push({ id: 970, user, body: "Thanks for the update!", updated_at: "2026-10-01T19:00:00Z" });
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  assert.equal(eventTarget(api, { action: "created", issue: { number: 10, pull_request: {} }, comment: { id: 970 } }, "issue_comment", {}), null);
  api.reviewThreads = [{
    id: "setup-thread", isResolved: false, comments: [{
      id: 971, user: { login: "chatgpt-codex-connector[bot]", type: "Bot" },
      body: "To use Codex here, [create a Codex account and connect to github](https://chatgpt.com/codex/cloud/settings/connectors).",
      updated_at: "2026-10-01T19:01:00Z",
    }],
  }];
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  api.reviews.push({ id: 972, user, state: "COMMENTED", body: "Please add an empty-state regression.", submitted_at: "2026-10-01T19:02:00Z" });
  assert.equal(prepare(api, { number: 10, automatic: true }, APP).decision.mode, "code");
});

test("approval and empty review bodies do not request coding", () => {
  const api = readySession();
  api.reviews.push({ id: 975, user, state: "APPROVED", body: "Looks good!", submitted_at: "2026-10-01T19:03:00Z" });
  api.reviews.push({ id: 976, user, state: "COMMENTED", body: "", submitted_at: "2026-10-01T19:04:00Z" });
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
});

test("overlapping slash and CI events route to one root and re-admit after publication", () => {
  const api = readySession();
  api.reviews.push({ id: 980, user, state: "COMMENTED", body: "Please add a regression.", submitted_at: "2026-10-01T20:00:00Z" });
  const comment = eventTarget(api, { action: "created", issue: { number: 9 }, comment: { id: 1 } }, "issue_comment", {});
  const ci = eventTarget(api, { sha: api.pr.head.sha }, "status", {});
  // Both are routed before the first worker publishes. The canonical lock key
  // is independent of issue/PR number, event kind and commit SHA.
  assert.equal(sessionRoot(api, comment, APP), 9);
  assert.equal(sessionRoot(api, ci, APP), 9);
  assert.equal(comment.number, 9);
  assert.equal(ci.number, 10);
  const calls = api.calls.length;
  sessionRoot(api, ci, APP);
  assert.equal(api.calls.length, calls);
  const first = prepare(api, comment, APP);
  assert.equal(first.decision.mode, "code");
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  publish(api, first, work, APP, { runId: "44" });
  // Admission is inside the called-workflow lock, not cached during routing.
  assert.equal(prepare(api, ci, APP), null);
  assert.equal(stateFrom(api.edits(api.comments), APP).rounds, 2);
});

test("a new failing check on a previously green head runs once per failure", () => {
  const api = readySession();
  api.checks[0].conclusion = "failure";
  const first = prepare(api, { number: 10, automatic: true }, APP);
  assert.equal(first.decision.mode, "code");
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  publish(api, first, work, APP, { runId: "44" });
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  api.checks[0].id += 1;
  assert.equal(prepare(api, { number: 10, automatic: true }, APP).decision.mode, "code");
});

test("only anchored, nonempty slash commands select supported models", () => {
  assert.equal(command("/astra implement this").model, "gpt-6-astra");
  assert.equal(command("/sol fix it\r\nkeep scope").model, "gpt-6-sol");
  assert.equal(command("/terra explain this").model, "gpt-5.6-terra");
  for (const text of [
    "/astra",
    "@astra fix",
    "> /astra fix",
    "```\n/astra fix\n```",
    "/astral fix",
    "/solitude fix",
    "/terraform fix",
  ])
    assert.equal(command(text), null);
});

test("write roles and ordinary events cannot start an agent", () => {
  const api = new FakeAPI();
  api.roles.maintainer = "write";
  assert.equal(initial(api), null);
  api.roles.maintainer = "maintain";
  assert.equal(prepare(api, { number: 9, automatic: true }, APP), null);
});

test("a command without verified editor metadata cannot pass collection and admission", () => {
  const api = new FakeAPI();
  api.edits = (comments) => comments.map((comment) => ({ ...comment, editingVerified: false }));
  assert.equal(initial(api), null);
});

test("automatic events on an unrelated PR stop before reviews, roles and checks", () => {
  const api = new FakeAPI();
  api.pr = {
    number: 10,
    user,
    state: "open",
    body: "An ordinary contributor PR",
    base: { ref: "master" },
    head: { ref: "feature", sha: A, repo: { full_name: api.repository } },
  };
  api.prComments = [{ ...api.command, body: "/astra historical request" }];
  api.edits = () => { throw Error("comment editor metadata should not be fetched"); };
  api.threads = () => { throw Error("reviews should not be collected"); };
  api.role = () => { throw Error("roles should not be fetched"); };
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
});

test("the context size limit applies when a worker prompt is built", () => {
  const api = new FakeAPI();
  const plan = initial(api);
  plan.snapshot.sourceComments.push({ body: "x".repeat(512000) });
  assert.throws(() => prompt(plan), /Slash prompt exceeds the 512 KiB/);
});

test("issue command creates one draft PR with append-only commits and durable memory", () => {
  const api = new FakeAPI();
  const state = start(api);
  assert.equal(state.pr, 10);
  assert.equal(state.status, "waiting");
  assert.equal(state.lastHead, B);
  assert.equal(api.pr.draft, true);
  assert.match(api.pr.body, /- \[x\] 🐛 Bug fix/);
  assert.match(api.pr.body, /<!-- slash-description:start -->/);
  assert.equal(api.calls.filter((c) => c.path === "pulls").length, 1);
  assert.match(
    api.calls.find((c) => c.path === "git/commits").data.message,
    /Codex-Session: 11111111/,
  );
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  const session = stateFrom(api.comments, APP);
  assert.match(session.summary, /automation enable/);
});

test("unchanged issue work answers in the checkpoint without creating a PR", () => {
  const api = new FakeAPI(),
    work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  work.result.summary = "The existing behavior already covers the request.";
  const state = publish(api, initial(api), work, APP, { runId: "42" });
  assert.equal(state.status, "complete");
  assert.equal(state.pr, null);
  assert.ok(!api.calls.some((call) => call.path === "pulls"));
  const checkpoint = api.comments.find((comment) => comment.user === bot);
  assert.match(checkpoint.body, /Slash agent: \*\*complete\*\*/);
  assert.match(checkpoint.body, /existing behavior already covers/);
});

test("readiness waits for all required checks with the expected App, without requesting review", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  api.checks[0].app.id = 9;
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  api.checks[0].app.id = 15368;
  api.statuses = [{ id: 4, context: "CodeRabbit", state: "pending" }];
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  api.statuses[0].state = "success";
  const plan = prepare(api, { number: 10, automatic: true }, APP);
  assert.equal(plan.decision.mode, "ready");
  publish(api, plan, null, APP, { runId: "43", workerSucceeded: false });
  assert.equal(api.pr.draft, false);
  assert.ok(!JSON.stringify(api.calls).includes("requestReviews"));
  api.pr.mergeable = null;
  assert.equal(checksReady(collect(api, 10, APP)), false);
  api.pr.mergeable = true;
  api.statuses = [];
  assert.equal(checksReady(collect(api, 10, APP)), true);
});

test("readiness accepts wildcard integration rules but not unknown mergeability", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  let snapshot = collect(api, 10, APP);
  snapshot.requiredChecks[0].integration_id = -1;
  snapshot.checks[0].appId = 123;
  assert.equal(checksReady(snapshot), true);
  snapshot.pr.mergeable = null;
  assert.equal(checksReady(snapshot), false);
});

test("closed or locked work cannot resume", () => {
  for (const stop of ["closed issue", "locked issue", "closed PR"]) {
    const api = new FakeAPI();
    start(api);
    api.reviews.push({ id: 1999, user, state: "COMMENTED", body: "Please continue", submitted_at: "2026-09-15T18:00:00Z" });
    if (stop === "closed issue") api.issue.state = "closed";
    if (stop === "locked issue") api.issue.locked = true;
    if (stop === "closed PR") api.pr.state = "closed";
    assert.equal(prepare(api, { number: 10, automatic: true }, APP), null, stop);
  }
});

test("valid review feedback resumes the same branch, replies and resolves the supplied thread", () => {
  const api = new FakeAPI();
  start(api);
  api.pr.body += "\n\n<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\nReviewer-maintained section";
  api.reviewThreads = [
    {
      id: "thread-1",
      isResolved: false,
      comments: [
        {
          id: 900,
          body: "Missing regression",
          user: { login: "coderabbitai[bot]", type: "Bot" },
        },
      ],
    },
  ];
  const plan = prepare(api, { number: 10, automatic: true }, APP);
  assert.equal(plan.decision.mode, "code");
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  work.result.responses = [
    {
      thread_id: "thread-1",
      body: "Covered by the existing regression at test_scope.py.",
      resolve: true,
    },
  ];
  publish(api, plan, work, APP, { runId: "43" });
  assert.equal(api.reviewThreads[0].isResolved, true);
  assert.match(api.pr.body, /Reviewer-maintained section/);
  assert.equal(api.calls.filter((c) => c.path === "pulls").length, 1);
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
});

test("a new CI failure resumes work, while pending checks do not consume a turn", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  api.checks[0].status = "in_progress";
  api.checks[0].conclusion = null;
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  api.checks[0].status = "completed";
  api.checks[0].conclusion = "failure";
  assert.equal(
    prepare(api, { number: 10, automatic: true }, APP).decision.mode,
    "code",
  );
});

test("a second wakeup after a failed-check iteration does not buy another worker turn", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  api.checks[0].conclusion = "failure";
  const first = prepare(api, { number: 10, automatic: true }, APP);
  assert.equal(first.decision.mode, "code");
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  publish(api, first, work, APP, { runId: "43" });
  assert.equal(stateFrom(api.comments, APP).rounds, 2);
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
});

test("an agent reply left unresolved does not trigger another coding turn itself", () => {
  const api = new FakeAPI();
  start(api);
  api.reviewThreads = [
    {
      id: "thread-1",
      isResolved: false,
      comments: [{ id: 900, body: "Please clarify", user }],
    },
  ];
  const plan = prepare(api, { number: 10, automatic: true }, APP);
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  work.result.responses = [
    {
      thread_id: "thread-1",
      body: "The current test covers the stated behavior; please clarify the remaining concern.",
      resolve: false,
    },
  ];
  publish(api, plan, work, APP, { runId: "43" });
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  api.reviewThreads[0].comments.push({
    id: 902,
    body: "Please add the missing type validation",
    user,
  });
  assert.equal(
    prepare(api, { number: 10, automatic: true }, APP).decision.mode,
    "code",
  );
});

test("pause, resume, role revocation and iteration cap survive separate runs", () => {
  const api = new FakeAPI();
  start(api);
  api.command.body = "/sol pause";
  api.command.updated_at = "2026-09-15T11:00:00Z";
  let plan = initial(api);
  assert.equal(plan.decision.mode, "pause");
  publish(api, plan, null, APP, { runId: "43" });
  assert.equal(initial(api), null);
  api.command.body = "/sol resume";
  api.command.updated_at = "2026-09-15T12:00:00Z";
  plan = initial(api);
  assert.equal(plan.decision.mode, "code");
  assert.match(plan.decision.task, /agreed scope/);
  api.roles.maintainer = "write";
  assert.equal(initial(api), null);
  api.roles.maintainer = "maintain";
  const snapshot = collect(api, 9, APP);
  snapshot.session.rounds = 4;
  snapshot.session.commandUpdatedAt = api.command.updated_at;
  snapshot.session.status = "waiting";
  snapshot.session.handled = "unhandled-review-feedback";
  assert.equal(decide(snapshot, { automatic: true }).mode, "limit");
});

test("four published rounds exhaust the budget until a new maintainer command", () => {
  const api = new FakeAPI();
  start(api);
  for (let round = 2; round <= 4; round++) {
    api.reviews.push({
      id: 1000 + round,
      user,
      body: `Maintainer follow-up ${round}`,
      updated_at: `2026-09-15T1${round}:00:00Z`,
    });
    const plan = prepare(api, { number: 10, automatic: true }, APP);
    assert.equal(plan.decision.mode, "code");
    const work = artifact();
    work.changes = [];
    work.result.outcome = "unchanged";
    const state = publish(api, plan, work, APP, { runId: String(42 + round) });
    assert.equal(state.rounds, round);
  }
  api.reviews.push({
    id: 1005,
    user,
    body: "One more request after the budget",
    updated_at: "2026-09-15T20:00:00Z",
  });
  const exhausted = prepare(api, { number: 10, automatic: true }, APP);
  assert.equal(exhausted.decision.mode, "limit");
  assert.equal(publish(api, exhausted, null, APP, { runId: "50" }).rounds, 4);
  api.command.body = "/sol resume";
  api.command.updated_at = "2026-09-15T21:00:00Z";
  const resumed = initial(api);
  assert.equal(resumed.decision.mode, "code");
  assert.equal(resumed.decision.rounds, 0);
});

test("existing Sol checkpoints remain readable while new Sol commands use GPT-6", () => {
  const api = new FakeAPI();
  const state = start(api);
  state.model = "gpt-5.6-sol";
  api.comments.find((comment) => comment.id === 100).body = renderState(state, api.repository);
  assert.equal(stateFrom(api.edits(api.comments), APP).model, "gpt-5.6-sol");
  api.command.body = "/sol resume";
  api.command.updated_at = "2026-09-15T21:00:00Z";
  const plan = initial(api);
  assert.equal(plan.decision.parsed.model, "gpt-6-sol");
});

test("stale work, repo/App mismatch and protected branches perform no publication", () => {
  const api = new FakeAPI();
  const plan = initial(api);
  api.issue.body += " New scope";
  const skipped = publish(api, plan, artifact(), APP, { runId: "42" });
  assert.equal(skipped.skipped, true);
  assert.deepEqual(skipped.changed, ["issue"]);
  assert.deepEqual(snapshotDifferences(plan.snapshot, collect(api, 9, APP)), ["issue"]);
  assert.equal(api.calls.length, 0);
  assert.throws(
    () =>
      publish(api, { ...plan, repository: "other/repo" }, artifact(), APP, {
        runId: "42",
      }),
    /identity/,
  );
  assert.throws(
    () => publish(api, plan, artifact(), "other-app", { runId: "42" }),
    /identity/,
  );
  api.branches["agents/issue-9"] = A;
  api.protected = true;
  assert.throws(() => initial(api), /Protected/);
});

test("revoking the active command author cannot revive an older maintainer task", () => {
  const api = new FakeAPI();
  api.comments.unshift({
    ...api.command,
    id: 2,
    user: { login: "other", type: "User" },
    updated_at: "2026-09-14T10:00:00Z",
    body: "/sol an older task",
  });
  api.roles.other = "maintain";
  start(api);
  api.roles.maintainer = "write";
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
});

test("failed worker leaves a blocked checkpoint and cannot loop automatically", () => {
  const api = new FakeAPI();
  publish(api, initial(api), null, APP, {
    runId: "42",
    workerSucceeded: false,
  });
  assert.equal(stateFrom(api.comments, APP).status, "blocked");
  assert.equal(prepare(api, { number: 9, automatic: true }, APP), null);
  assert.ok(!api.calls.some((c) => c.path === "git/refs"));
});

test("a scope change during blob preparation prevents the branch write", () => {
  const api = new FakeAPI();
  const plan = initial(api);
  const write = api.write.bind(api);
  api.write = (path, data, method) => {
    const result = write(path, data, method);
    if (path === "git/blobs") api.issue.body = "A maintainer changed the scope";
    return result;
  };
  assert.throws(
    () => publish(api, plan, artifact(), APP, { runId: "42" }),
    /changed/,
  );
  assert.ok(
    !api.calls.some((c) => c.path === "git/refs" || c.path === "pulls"),
  );
});

test("collection refuses forks, unsafe branches and unowned session state", () => {
  const fork = new FakeAPI();
  start(fork);
  fork.pr.head.repo.full_name = "other/fork";
  assert.throws(() => collect(fork, 10, APP), /Fork PRs/);
  fork.pr.head.repo.full_name = fork.repository;
  const unsafe = new FakeAPI();
  unsafe.pr = {
    number: 10, user, state: "open", body: "ordinary PR",
    base: { ref: "master" },
    head: { ref: "master", sha: A, repo: { full_name: unsafe.repository } },
  };
  unsafe.prComments = [unsafe.command];
  assert.throws(() => collect(unsafe, 10, APP), /Unsafe target branch/);

  const unowned = new FakeAPI();
  unowned.branches["agents/issue-9"] = A;
  assert.throws(() => initial(unowned), /already exists without a session/);

  const missing = new FakeAPI();
  start(missing);
  missing.comments = [missing.command];
  assert.throws(() => collect(missing, 10, APP), /no owned session/);

  for (const [field, value, pattern] of [
    ["root", 8, /another issue/],
    ["pr", 11, /another PR/],
    ["branch", "other-branch", /does not match/],
  ]) {
    const api = new FakeAPI();
    const state = start(api);
    state[field] = value;
    api.comments.find((c) => c.id === 100).body = renderState(state, api.repository);
    assert.throws(() => collect(api, field === "root" ? 9 : 10, APP), pattern);
  }
});

test("publication rejects blocked writes, mismatched outcomes and branch takeover", () => {
  const blocked = new FakeAPI();
  start(blocked);
  blocked.reviewThreads = [{ id: "blocked-thread", isResolved: false, comments: [{ id: 900, user, body: "Explain" }] }];
  const blockedPlan = prepare(blocked, { number: 10, automatic: true }, APP);
  const blockedWork = artifact();
  blockedWork.changes = [];
  blockedWork.result.outcome = "blocked";
  blockedWork.result.responses = [{ thread_id: "blocked-thread", body: "Will answer", resolve: true }];
  assert.throws(() => publish(blocked, blockedPlan, blockedWork, APP, { runId: "43" }), /Blocked output/);

  const mismatch = new FakeAPI();
  const mismatchWork = artifact();
  mismatchWork.result.outcome = "unchanged";
  assert.throws(() => publish(mismatch, initial(mismatch), mismatchWork, APP, { runId: "42" }), /outcome disagrees/);

  const advanced = new FakeAPI();
  start(advanced);
  advanced.reviews.push({ id: 1002, user, state: "COMMENTED", body: "Please fix another case", submitted_at: "2026-09-15T15:00:00Z" });
  const plan = prepare(advanced, { number: 10, automatic: true }, APP);
  const write = advanced.write.bind(advanced);
  advanced.write = (path, data, method) => {
    const result = write(path, data, method);
    if (path === "git/trees") advanced.branches["agents/issue-9"] = "c".repeat(40);
    return result;
  };
  assert.throws(() => publish(advanced, plan, artifact(), APP, { runId: "43" }), /Branch advanced/);

  const adopted = new FakeAPI();
  const pages = adopted.pages.bind(adopted);
  adopted.pages = (path) => path.startsWith("pulls?")
    ? [{ number: 10, user: { login: "other[bot]" }, body: "foreign" }]
    : pages(path);
  assert.throws(() => start(adopted), /ownership mismatch/);
});

test("checkpoint identity cannot be forged by a human or another bot", () => {
  const api = new FakeAPI();
  const state = start(api);
  const body = renderState(state, api.repository);
  assert.equal(stateFrom([{ user, body }], APP), null);
  assert.equal(
    stateFrom([{ user: { type: "Bot", login: "other[bot]" }, body }], APP),
    null,
  );
  assert.throws(
    () =>
      stateFrom(
        [
          { user: bot, body },
          { user: bot, body },
        ],
        APP,
      ),
    /Multiple/,
  );
});

test("a failed PR creation recovers its owned branch without requiring more code changes", () => {
  const api = new FakeAPI();
  const write = api.write.bind(api);
  let failOnce = true;
  api.write = (path, data, method) => {
    if (path === "pulls" && failOnce) {
      failOnce = false;
      throw Error("Temporary PR creation failure");
    }
    return write(path, data, method);
  };
  assert.throws(() => start(api), /Temporary/);
  const plan = prepare(api, { number: 9, automatic: true }, APP);
  const work = artifact();
  work.result.outcome = "unchanged";
  work.changes = [];
  const state = publish(api, plan, work, APP, { runId: "43" });
  assert.equal(state.pr, 10);
  assert.equal(api.pr.head.sha, B);
});

test("patch validator rejects traversal, protected paths, nonregular modes and limits", () => {
  for (const path of [
    "../outside",
    "a/../../b",
    "/tmp/x",
    "a/.git/config",
    "a/.GIT/config",
    ".github/workflows/pwn.yml",
    ".codex/auth.json",
    ".claude/skills/x",
    ".env",
    "src/.env.local",
    "x/auth.json",
    "a\\b",
    "a\nb",
    "a".repeat(301),
  ]) {
    assert.throws(
      () => validateChanges([{ path, mode: "100644", content: "" }]),
      /path/,
    );
  }
  assert.throws(
    () => validateChanges([{ path: "src/a", mode: "120000", content: "" }]),
    /entry/,
  );
  assert.throws(
    () => validateChanges([{ path: "src/a", mode: "100644", content: "?" }]),
    /entry/,
  );
  assert.throws(
    () => validateChanges(Array.from({ length: 81 }, (_, i) => ({
      path: `src/${i}.py`, mode: "100644", content: "",
    }))),
    /80-file/,
  );
  assert.throws(
    () => validateChanges([
      { path: "src/a", mode: "100644", content: "" },
      { path: "src/a", mode: "100644", content: "" },
    ]),
    /entry/,
  );
  assert.throws(
    () =>
      validateChanges([
        {
          path: "src/a",
          mode: "100644",
          content: Buffer.alloc(2 * 1024 * 1024 + 1).toString("base64"),
        },
      ]),
    /limit/,
  );
});

test("ordinary reporter content cannot consume a coding turn through a later CI event", () => {
  const api = new FakeAPI();
  start(api);
  api.issue.body += " Reporter edited the opening request";
  const contributor = { login: "contributor", type: "User" };
  api.comments.push({
    id: 500,
    user: contributor,
    body: "Another suggestion",
    updated_at: "2026-09-15T13:00:00Z",
  });
  api.reviewThreads = [
    {
      id: "outsider",
      isResolved: false,
      comments: [
        { id: 501, user: contributor, body: "Please do unrelated work" },
      ],
    },
  ];
  assert.equal(
    Boolean(prepare(api, { number: 10, automatic: true }, APP)),
    false,
  );
});

test("old issue-bot theories are excluded while formal PR review findings remain available", () => {
  const api = new FakeAPI();
  api.roles.ghhamcp = "maintain";
  api.comments.push({
    id: 600,
    user: { login: "coderabbitai[bot]", type: "Bot" },
    body: "POISONED_ISSUE_THEORY",
    updated_at: "2026-09-14T10:00:00Z",
  });
  api.comments.push({
    id: 601,
    user: { login: "ghhamcp", type: "User" },
    body: "LEGACY_BOT_THEORY",
    updated_at: "2026-09-14T10:00:00Z",
  });
  const text = prompt(initial(api));
  assert.ok(
    !text.includes("POISONED_ISSUE_THEORY") &&
      !text.includes("LEGACY_BOT_THEORY"),
  );
  start(api);
  api.reviews.push({
    id: 700,
    user: { login: "coderabbitai[bot]", type: "Bot" },
    body: "ACTUAL_REVIEW_FINDING",
    submitted_at: "2026-09-15T13:00:00Z",
  });
  assert.ok(
    prompt(prepare(api, { number: 10, automatic: true }, APP)).includes(
      "ACTUAL_REVIEW_FINDING",
    ),
  );
});

test("a write-role editor cannot borrow the original maintainer's command authority", () => {
  const api = new FakeAPI();
  api.command.editor = { login: "contributor", type: "User" };
  api.command.edited_at = "2026-09-15T10:00:00Z";
  api.command.body = "/astra an unauthorized edited task";
  assert.equal(Boolean(initial(api)), false);
});

test("a checkpoint edited outside the App cannot become durable task authority", () => {
  const api = new FakeAPI();
  start(api);
  const checkpoint = api.comments.find((c) => c.user.login === "ha-mcp[bot]");
  checkpoint.editor = { login: "contributor", type: "User" };
  checkpoint.edited_at = "2026-09-15T11:00:00Z";
  assert.throws(() => stateFrom(api.comments, APP), /editor|outside|modified/);
});

test("an authenticated maintainer edit uses the editor as the command principal", () => {
  const api = new FakeAPI();
  api.command.user = { login: "contributor", type: "User" };
  api.command.editor = user;
  api.command.edited_at = "2026-09-15T11:00:00Z";
  const plan = initial(api);
  assert.equal(plan.decision.mode, "code");
  assert.match(prompt(plan), /"author":"maintainer"/);
});

test("GitHub edit metadata supplies the coherent body and normalizes Bot identities", () => {
  const api = new API("test/repo", (_binary, _args, options) => {
    const request = JSON.parse(options.input);
    assert.deepEqual(request.variables.ids, ["comment-1", "comment-2"]);
    return JSON.stringify({
      data: {
        nodes: [
          {
            id: "comment-1",
            body: "/astra edited content",
            updatedAt: "2026-09-15T12:00:00Z",
            lastEditedAt: "2026-09-15T12:00:00Z",
            author: { login: "maintainer", __typename: "User" },
            editor: { login: "contributor", __typename: "User" },
          },
          {
            id: "comment-2",
            body: "Review finding",
            updatedAt: "2026-09-15T12:00:00Z",
            lastEditedAt: null,
            author: { login: "coderabbitai", __typename: "Bot" },
            editor: null,
          },
        ],
      },
    });
  });
  const comments = api.edits([
    { node_id: "comment-1", body: "old body" },
    { node_id: "comment-2" },
  ]);
  assert.equal(comments[0].body, "/astra edited content");
  assert.equal(comments[0].editor.login, "contributor");
  assert.equal(comments[1].user.login, "coderabbitai[bot]");
  assert.ok(comments.every((c) => c.editingVerified));
});

test("missing edit metadata fails closed instead of trusting the original author", () => {
  const api = new API("test/repo", () =>
    JSON.stringify({
      data: {
        nodes: [
          {
            id: "comment-1",
            body: "/astra forged",
            author: { login: "maintainer", __typename: "User" },
          },
        ],
      },
    }),
  );
  assert.throws(() => api.edits([{ node_id: "comment-1" }]), /metadata/);
});

for (const stage of ["resolution", "summary", "checkpoint"]) {
  test(`review publication resumes after a ${stage} failure without duplicate replies or summaries`, () => {
    const api = new FakeAPI();
    start(api);
    api.reviewThreads = [
      {
        id: "retry",
        isResolved: false,
        comments: [{ id: 800, user, body: "Explain this fix" }],
      },
    ];
    const plan = prepare(api, { number: 10, automatic: true }, APP),
      work = artifact();
    work.changes = [];
    work.result.outcome = "unchanged";
    work.result.responses = [
      {
        thread_id: "retry",
        body: "Verified by the existing regression.",
        resolve: true,
      },
    ];
    let failOnce = true;
    const write = api.write.bind(api),
      graphql = api.graphql.bind(api);
    api.graphql = (query, variables) => {
      if (
        stage === "resolution" &&
        failOnce &&
        query.includes("resolveReviewThread")
      ) {
        failOnce = false;
        throw Error("Injected resolution failure");
      }
      return graphql(query, variables);
    };
    api.write = (path, data, method) => {
      if (
        stage === "checkpoint" &&
        failOnce &&
        path === "issues/comments/100" &&
        data.body.includes("**waiting**")
      ) {
        failOnce = false;
        throw Error("Injected checkpoint failure");
      }
      const value = write(path, data, method);
      if (stage === "summary" && failOnce && path === "issues/10/comments") {
        failOnce = false;
        throw Error("Injected summary failure");
      }
      return value;
    };
    assert.throws(
      () => publish(api, plan, work, APP, { runId: "43" }),
      /Injected/,
    );
    const next = prepare(api, { number: 10, automatic: true }, APP);
    assert.ok(next, "Partially published work must remain resumable");
    if (stage !== "resolution") work.result.responses = [];
    else
      work.result.responses[0].body =
        "Clarified evidence from the regression after retry.";
    work.result.summary =
      "Recovered review publication with verified evidence.";
    publish(api, next, work, APP, { runId: "44" });
    assert.equal(api.reviewThreads[0].comments.length, 2);
    assert.equal(api.prComments.length, 1);
    assert.equal(stateFrom(api.comments, APP).status, "waiting");
  });
}

test("a no-code review resolution becomes ready when its checks already passed", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  api.reviewThreads = [
    {
      id: "explain",
      isResolved: false,
      comments: [{ id: 800, user, body: "Explain the existing test" }],
    },
  ];
  const plan = prepare(api, { number: 10, automatic: true }, APP),
    work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  work.result.responses = [
    {
      thread_id: "explain",
      body: "The existing regression covers it.",
      resolve: true,
    },
  ];
  publish(api, plan, work, APP, { runId: "43" });
  assert.equal(api.pr.draft, false);
});

test("automatic agent-instruction entrypoints require a human-controlled patch", () => {
  for (const path of ["AGENTS.md", "src/AGENTS.md", "src/nested/CLAUDE.md"]) {
    assert.throws(
      () => validateChanges([{ path, mode: "100644", content: "" }]),
      /path/,
    );
  }
});

test("a maintainer can authorize feedback in a contributor-opened thread", () => {
  const api = new FakeAPI();
  start(api);
  api.reviewThreads = [
    {
      id: "mixed",
      isResolved: false,
      comments: [
        {
          id: 700,
          user: { login: "contributor", type: "User" },
          body: "Suggestion",
        },
        { id: 701, user, body: "Please implement this correction" },
      ],
    },
  ];
  const snapshot = collect(api, 10, APP),
    result = response();
  result.responses = [
    {
      thread_id: "mixed",
      body: "Correction verified by a regression.",
      resolve: true,
    },
  ];
  assert.doesNotThrow(() => validateResult(result, snapshot));
});

test("blocked review work posts its clarification and one PR summary without resolving", () => {
  const api = new FakeAPI();
  start(api);
  api.reviewThreads = [
    {
      id: "clarify",
      isResolved: false,
      comments: [{ id: 800, user, body: "Please choose a new scope" }],
    },
  ];
  const plan = prepare(api, { number: 10, automatic: true }, APP),
    work = artifact();
  work.changes = [];
  work.result.outcome = "blocked";
  work.result.responses = [
    {
      thread_id: "clarify",
      body: "Which of the two behaviors should be supported?",
      resolve: false,
    },
  ];
  const state = publish(api, plan, work, APP, { runId: "43" });
  assert.equal(state.status, "blocked");
  assert.equal(api.reviewThreads[0].comments.length, 2);
  assert.equal(api.prComments.length, 1);
  assert.equal(api.reviewThreads[0].isResolved, false);
});

test("inline feedback edited while blobs are prepared prevents a branch update", () => {
  const api = new FakeAPI();
  start(api);
  api.reviewThreads = [
    {
      id: "change",
      isResolved: false,
      comments: [{ id: 800, user, body: "Fix this" }],
    },
  ];
  const plan = prepare(api, { number: 10, automatic: true }, APP);
  const write = api.write.bind(api);
  api.write = (path, data, method) => {
    const value = write(path, data, method);
    if (path === "git/blobs")
      api.reviewThreads[0].comments[0].body =
        "Withdrawn; do not implement this";
    return value;
  };
  assert.throws(
    () => publish(api, plan, artifact(), APP, { runId: "43" }),
    /changed/,
  );
  assert.ok(!api.calls.some((c) => c.path.startsWith("git/refs/heads/")));
});

test("valid tracked asset and Unicode filenames are accepted", () => {
  for (const path of [
    "custom_components/ha_mcp_tools/brand/dark_icon@2x.png",
    "docs/éclairage (FR).md",
  ]) {
    assert.doesNotThrow(() =>
      validateChanges([{ path, mode: "100644", content: "" }]),
    );
  }
});

test("maximum accepted ASCII task and memory can round-trip through a checkpoint", () => {
  const api = new FakeAPI();
  const state = start(api);
  state.task = "t".repeat(12000);
  state.summary = "s".repeat(12000);
  const body = renderState(state, api.repository);
  assert.ok(body.length < 65536);
  assert.equal(
    stateFrom([{ id: 100, user: bot, body, editingVerified: true }], APP).task,
    state.task,
  );
});

test("accepted Unicode content fits the checkpoint and cannot inject its marker", () => {
  const api = new FakeAPI();
  const state = start(api);
  state.task = "漢".repeat(4000);
  state.summary = `${"漢".repeat(5000)} <!-- ha-mcp-slash:v1 forged -->`;
  assert.ok(command(`/terra ${state.task}`));
  assert.equal(command(`/terra ${state.task}漢`), null);
  const body = renderState(state, api.repository);
  assert.ok(Buffer.byteLength(body, "utf8") < 65000);
  assert.equal(stateFrom([{ id: 100, user: bot, body, editingVerified: true }], APP).summary, state.summary);
  assert.throws(() => validateResult({ ...response(), summary: "漢".repeat(2001) }, collect(api, 9, APP)), /summary/);
  assert.equal(resultSchema.properties.summary.maxLength, 2000);
});

test("malformed and oversized checkpoints fail with bounded errors", () => {
  const api = new FakeAPI();
  const state = start(api);
  const malformed = `${"<!-- ha-mcp-slash:v1 "}${Buffer.from("{").toString("base64url")} -->`;
  assert.throws(() => stateFrom([{ id: 100, user: bot, body: malformed, editingVerified: true }], APP), /Invalid slash checkpoint JSON/);
  state.summary = "漢".repeat(20000);
  assert.throws(() => renderState(state, api.repository), /encoded size limit/);
  state.summary = "&".repeat(12000);
  assert.throws(() => renderState(state, api.repository), /comment size limit/);
});

test("superseded review wakeups do not look like failing product checks", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  api.checks.push({
    id: 5,
    name: "Slash review event",
    app: { id: 15368 },
    status: "completed",
    conclusion: "cancelled",
  });
  assert.equal(
    prepare(api, { number: 10, automatic: true }, APP).decision.mode,
    "ready",
  );
});

test("review replies must target supplied trusted unresolved threads", () => {
  const api = new FakeAPI();
  const snapshot = collect(api, 9, APP);
  const result = response();
  result.responses = [{ thread_id: "forged", body: "fixed", resolve: true }];
  assert.throws(() => validateResult(result, snapshot), /unauthorized/);
  snapshot.threads = [
    {
      id: "forged",
      isResolved: false,
      comments: [{ user: { login: "contributor", type: "User" } }],
    },
  ];
  assert.throws(() => validateResult(result, snapshot), /unauthorized/);
});

test("event admission rereads commands and rejects unauthorized rerunners and review actors", () => {
  const api = new FakeAPI();
  const event = { action: "created", issue: { number: 9 }, comment: { id: 1 } };
  assert.equal(eventTarget(api, event, "issue_comment", {}).commandId, 1);
  assert.equal(
    eventTarget(api, event, "issue_comment", {
      GITHUB_ACTOR: "maintainer",
      GITHUB_TRIGGERING_ACTOR: "contributor",
    }),
    null,
  );
  api.roles.maintainer = "write";
  assert.equal(eventTarget(api, event, "issue_comment", {}), null);
  api.runs[5] = {
    status: "completed",
    path: ".github/workflows/slash-agent-review-event.yml",
    head_repository: { full_name: api.repository },
    actor: { login: "contributor", type: "User" },
    head_sha: A,
  };
  assert.equal(
    eventTarget(api, { workflow_run: { id: 5 } }, "workflow_run", {}),
    null,
  );
});

test("manual dispatch and status wakeups require exact authority and one current same-repo PR", () => {
  const api = new FakeAPI();
  const manual = { inputs: { issue_number: "9", comment_id: "1" } };
  assert.deepEqual(eventTarget(api, manual, "workflow_dispatch", { GITHUB_ACTOR: "maintainer" }), {
    number: 9, commandId: 1, automatic: false,
  });
  assert.equal(eventTarget(api, manual, "workflow_dispatch", { GITHUB_ACTOR: "contributor" }), null);
  for (const [issue_number, comment_id] of [["0", "1"], ["9", "0"], ["1.2", "1"], ["9", "Infinity"]])
    assert.throws(() => eventTarget(api, { inputs: { issue_number, comment_id } }, "workflow_dispatch", { GITHUB_ACTOR: "maintainer" }), /Invalid dispatch/);

  start(api);
  assert.equal(eventTarget(api, { sha: "bad" }, "status", {}), null);
  assert.equal(eventTarget(api, { sha: A }, "status", {}), null);
  assert.deepEqual(eventTarget(api, { sha: B }, "status", {}), {
    number: 10, commandId: null, automatic: true,
  });
  api.pr.state = "closed";
  assert.equal(eventTarget(api, { sha: B }, "status", {}), null);
  api.pr.state = "open";
  api.pr.head.repo.full_name = "other/fork";
  assert.equal(eventTarget(api, { sha: B }, "status", {}), null);
  api.pr.head.repo.full_name = api.repository;
  const pages = api.pages.bind(api);
  api.pages = (path) => path.startsWith("commits/") ? [api.pr, api.pr] : pages(path);
  assert.equal(eventTarget(api, { sha: B }, "status", {}), null);
});

test("workflow completion wakeups reject unknown paths, forks and incomplete runs", () => {
  const api = new FakeAPI();
  start(api);
  const run = {
    status: "completed",
    path: ".github/workflows/pr.yml@refs/heads/master",
    head_repository: { full_name: api.repository },
    actor: user,
    head_sha: B,
  };
  api.runs[6] = run;
  const event = { workflow_run: { id: 6 } };
  assert.equal(eventTarget(api, event, "workflow_run", {}).number, 10);
  run.status = "in_progress";
  assert.equal(eventTarget(api, event, "workflow_run", {}), null);
  run.status = "completed";
  run.path = ".github/workflows/unknown.yml";
  assert.equal(eventTarget(api, event, "workflow_run", {}), null);
  run.path = ".github/workflows/pr.yml";
  run.head_repository.full_name = "other/fork";
  assert.equal(eventTarget(api, event, "workflow_run", {}), null);
});

test("real API adapters paginate comments and review threads with explicit limits", () => {
  const api = new API("test/repo");
  let pageCalls = 0;
  api.get = () => {
    pageCalls += 1;
    return pageCalls === 1 ? Array.from({ length: 100 }, (_, i) => i) : [100];
  };
  assert.equal(api.pages("issues/9/comments").length, 101);
  api.get = () => ({ wrong: true });
  assert.throws(() => api.pages("issues/9/comments"), /Invalid paginated/);
  api.get = () => Array.from({ length: 100 }, (_, i) => i);
  assert.throws(() => api.pages("issues/9/comments"), /pagination limit/);

  const edits = Array.from({ length: 101 }, (_, i) => ({ node_id: `node-${i}` }));
  let batches = 0;
  api.graphql = (_, variables) => {
    batches += 1;
    return { nodes: variables.ids.map((id) => ({
      id, body: "review", updatedAt: "2026-09-15T00:00:00Z",
      lastEditedAt: null, editor: null,
      author: { login: "maintainer", __typename: "User" },
    })) };
  };
  assert.equal(api.edits(edits).length, 101);
  assert.equal(batches, 2);

  let threadQueries = 0;
  api.graphql = (query) => {
    threadQueries += 1;
    if (query.includes("reviewThreads"))
      return { repository: { pullRequest: { reviewThreads: {
        nodes: [{ id: "thread-1", isResolved: false, comments: {
          nodes: Array.from({ length: 100 }, (_, i) => ({
            databaseId: i + 1, body: "review", updatedAt: "2026-09-15T00:00:00Z",
            lastEditedAt: null, editor: null,
            author: { login: "maintainer", __typename: "User" },
          })),
          pageInfo: { hasNextPage: true, endCursor: "c1" },
        } }],
        pageInfo: { hasNextPage: false, endCursor: null },
      } } } };
    return { node: { comments: {
      nodes: [{ databaseId: 101, body: "late reply", updatedAt: "2026-09-15T01:00:00Z",
        lastEditedAt: null, editor: null,
        author: { login: "maintainer", __typename: "User" } }],
      pageInfo: { hasNextPage: false, endCursor: null },
    } } };
  };
  const threads = api.threads(10);
  assert.equal(threads[0].comments.length, 101);
  assert.equal(threads[0].comments.at(-1).body, "late reply");
  assert.equal(threadQueries, 2);
  api.graphql = () => ({ repository: { pullRequest: null } });
  assert.throws(() => api.threads(10), /Review thread response/);
});

test("review pagination stops when a thread or its comment history exceeds bounds", () => {
  const api = new API("test/repo");
  const comment = { databaseId: 1, body: "review", updatedAt: "2026-09-15T00:00:00Z",
    lastEditedAt: null, editor: null,
    author: { login: "maintainer", __typename: "User" } };
  api.graphql = (query) => query.includes("reviewThreads")
    ? { repository: { pullRequest: { reviewThreads: {
      nodes: [{ id: "thread-1", isResolved: false, comments: {
        nodes: Array.from({ length: 100 }, () => comment),
        pageInfo: { hasNextPage: true, endCursor: "next" },
      } }],
      pageInfo: { hasNextPage: false, endCursor: null },
    } } } }
    : { node: { comments: {
      nodes: Array.from({ length: 100 }, () => comment),
      pageInfo: { hasNextPage: true, endCursor: "next" },
    } } };
  assert.throws(() => api.threads(10), /Review thread is too large/);

  api.graphql = () => ({ repository: { pullRequest: { reviewThreads: {
    nodes: Array.from({ length: 100 }, (_, i) => ({
      id: `thread-${i}`, isResolved: false,
      comments: { nodes: [comment], pageInfo: { hasNextPage: false, endCursor: null } },
    })),
    pageInfo: { hasNextPage: true, endCursor: "next" },
  } } } });
  assert.throws(() => api.threads(10), /Too many review threads/);
});

test("workflow entrypoint wires admit, prompt, package and publish artifacts", () => {
  const directory = mkdtempSync(join(tmpdir(), "slash-main-"));
  const api = new FakeAPI();
  const eventPath = join(directory, "event.json");
  const outputPath = join(directory, "github-output.txt");
  const resultPath = join(directory, "model-result.json");
  const logPath = join(directory, "codex.log");
  const stateDirectory = join(directory, "state");
  writeFileSync(eventPath, JSON.stringify({ action: "created", issue: { number: 9 }, comment: { id: 1 } }));
  writeFileSync(outputPath, "");
  writeFileSync(resultPath, JSON.stringify(response()));
  writeFileSync(logPath, JSON.stringify({ type: "thread.started", thread_id: artifact().threadId }));
  const env = {
    GITHUB_REPOSITORY: api.repository,
    GITHUB_EVENT_NAME: "issue_comment",
    GITHUB_EVENT_PATH: eventPath,
    GITHUB_OUTPUT: outputPath,
    HA_MCP_APP_SLUG: APP,
    SLASH_STATE_DIR: stateDirectory,
    SLASH_SESSION_ROOT: "9",
    OUTPUT_PATH: resultPath,
    CODEX_LOG_PATH: logPath,
    TOKEN_APP_SLUG: APP,
    WORKER_RESULT: "success",
    GITHUB_RUN_ID: "42",
  };
  const createApi = () => api;
  try {
    main("route", env, { createApi });
    assert.match(readFileSync(outputPath, "utf8"), /root=9/);
    assert.equal(api.calls.length, 0);
    assert.throws(() => main("admit", { ...env, SLASH_SESSION_ROOT: "10" }, { createApi }), /Session root changed/);
    main("admit", env, { createApi });
    assert.match(readFileSync(outputPath, "utf8"), /run=true/);
    main("prompt", env, { createApi });
    assert.match(readFileSync(join(stateDirectory, "prompt.txt"), "utf8"), /Authenticated maintainer task/);
    assert.ok(JSON.parse(readFileSync(join(stateDirectory, "schema.json"), "utf8")));
    let packaged = false;
    main("package", env, { createApi, packageWorkFn: (...args) => {
      packaged = true;
      assert.equal(args[2].title, response().title);
      return artifact();
    } });
    assert.equal(packaged, true);
    let published = false;
    main("publish", env, { createApi, publishFn: (_api, plan, work, app, options) => {
      published = true;
      assert.equal(plan.repository, api.repository);
      assert.equal(work.threadId, artifact().threadId);
      assert.equal(app, APP);
      assert.equal(options.workerSucceeded, true);
      return { status: "waiting" };
    } });
    assert.equal(published, true);
    main("publish", { ...env, WORKER_RESULT: "failure" }, { createApi, publishFn: (_api, _plan, work, _app, options) => {
      assert.equal(work, null);
      assert.equal(options.workerSucceeded, false);
      return { status: "blocked" };
    } });
    assert.throws(() => main("publish", { ...env, TOKEN_APP_SLUG: "other" }, { createApi }), /Unexpected publication App/);
    assert.throws(() => main("publish", env, { createApi, publishFn: () => ({ skipped: true, changed: ["issue"] }) }), /Stale work skipped: issue/);
    assert.throws(() => main("admit", { ...env, GITHUB_OUTPUT: "" }, { createApi }), /GITHUB_OUTPUT is required/);
    assert.throws(() => main("admit", { ...env, HA_MCP_APP_SLUG: "BAD!" }, { createApi }), /Invalid HA_MCP_APP_SLUG/);
    main("admit", { ...env, HA_MCP_APP_SLUG: "" }, { createApi });
    assert.match(readFileSync(outputPath, "utf8"), /run=false/);
    writeFileSync(resultPath, "private-token-DO-NOT-PRINT invalid JSON");
    assert.throws(
      () => main("package", env, { createApi }),
      (error) => error.message === "Cannot read valid model result for package",
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("packaging captures modifications, deletions and new files without running repository hooks", () => {
  const directory = mkdtempSync(join(tmpdir(), "slash-agent-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: directory, encoding: "utf8" });
  try {
    git("init", "-q");
    git("config", "user.name", "Fixture");
    git("config", "user.email", "fixture@example.invalid");
    writeFileSync(join(directory, "old.txt"), "old");
    writeFileSync(join(directory, "delete.txt"), "delete");
    git("add", ".");
    git("commit", "-qm", "fixture");
    const head = git("rev-parse", "HEAD").trim();
    writeFileSync(join(directory, "old.txt"), "new");
    rmSync(join(directory, "delete.txt"));
    writeFileSync(join(directory, "new.txt"), "added");
    const plan = { snapshot: { head, threads: [] } };
    const packed = packageWork(
      directory,
      plan,
      response(),
      JSON.stringify({
        type: "thread.started",
        thread_id: artifact().threadId,
      }),
    );
    assert.deepEqual(packed.changes.map((f) => f.path).sort(), [
      "delete.txt",
      "new.txt",
      "old.txt",
    ]);
    assert.equal(
      packed.changes.find((f) => f.path === "delete.txt").content,
      null,
    );
    mkdirSync(join(directory, ".github"));
    writeFileSync(join(directory, ".github", "bad.txt"), "bad");
    assert.throws(() => packageWork(directory, plan, response()), /prohibited/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("packaging refuses symlinked files and directory components", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "slash-links-"));
  const outside = mkdtempSync(join(tmpdir(), "slash-outside-"));
  const git = (...args) => execFileSync("git", args, { cwd: directory, encoding: "utf8" });
  try {
    git("init", "-q");
    git("config", "user.name", "Fixture");
    git("config", "user.email", "fixture@example.invalid");
    mkdirSync(join(directory, "docs", "nested"), { recursive: true });
    writeFileSync(join(directory, "docs", "nested", "secret.txt"), "safe");
    git("add", ".");
    git("commit", "-qm", "fixture");
    const head = git("rev-parse", "HEAD").trim();
    const log = JSON.stringify({ type: "thread.started", thread_id: artifact().threadId });
    const plan = { snapshot: { head, threads: [] } };
    const outsideFile = join(outside, "secret.txt");
    writeFileSync(outsideFile, "private fixture content");
    try {
      symlinkSync(outsideFile, join(directory, "file-link"), "file");
    } catch (error) {
      if (process.platform === "win32" && ["EPERM", "EACCES"].includes(error.code)) {
        t.skip("Windows symlink creation is unavailable");
        return;
      }
      throw error;
    }
    assert.throws(() => packageWork(directory, plan, response(), log), /Symlink in patch path/);
    rmSync(join(directory, "file-link"));
    rmSync(join(directory, "docs", "nested"), { recursive: true });
    symlinkSync(outside, join(directory, "docs", "nested"), "dir");
    assert.throws(() => packageWork(directory, plan, response(), log), /Symlink in patch path/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
    rmSync(outside, { recursive: true, force: true });
  }
});
