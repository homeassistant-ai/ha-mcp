import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import {
  command,
  decide,
  digest,
  feedbackHash,
  renderState,
  stateFrom,
  validateChanges,
  validateResult,
} from "../../.github/slash-agent/core.mjs";
import { collect, eventTarget } from "../../.github/slash-agent/github.mjs";
import { prepare, prompt } from "../../.github/slash-agent/main.mjs";
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
    return structuredClone(this.reviewThreads);
  }
  write(path, data, method = "POST") {
    this.calls.push({ path, data: structuredClone(data), method });
    if (path === "issues/9/comments") {
      const c = { id: 100, user: bot, ...data };
      this.comments.push(c);
      return c;
    }
    if (path === "issues/comments/100") {
      Object.assign(
        this.comments.find((c) => c.id === 100),
        data,
      );
      return {};
    }
    if (path === "issues/10/comments") {
      const comment = { id: 1000 + this.prComments.length, user: bot, ...data };
      this.prComments.push(comment);
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

test("only anchored, nonempty slash commands select supported models", () => {
  assert.equal(command("/astra implement this").model, "gpt-6-astra");
  assert.equal(command("/sol fix it\r\nkeep scope").model, "gpt-5.6-sol");
  for (const text of [
    "/astra",
    "@astra fix",
    "> /astra fix",
    "```\n/astra fix\n```",
    "/astral fix",
    "/solitude fix",
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

test("issue command creates one draft PR with append-only commits and durable memory", () => {
  const api = new FakeAPI();
  const state = start(api);
  assert.equal(state.pr, 10);
  assert.equal(state.status, "waiting");
  assert.equal(state.lastHead, B);
  assert.equal(api.pr.draft, true);
  assert.equal(api.calls.filter((c) => c.path === "pulls").length, 1);
  assert.match(
    api.calls.find((c) => c.path === "git/commits").data.message,
    /Codex-Session: 11111111/,
  );
  assert.equal(prepare(api, { number: 10, automatic: true }, APP), null);
  const session = stateFrom(api.comments, APP);
  assert.match(session.summary, /automation enable/);
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
});

test("valid review feedback resumes the same branch, replies and resolves the supplied thread", () => {
  const api = new FakeAPI();
  start(api);
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
  assert.equal(decide(snapshot, { automatic: true }).mode, "limit");
});

test("stale work, repo/App mismatch and protected branches perform no publication", () => {
  const api = new FakeAPI();
  const plan = initial(api);
  api.issue.body += " New scope";
  assert.equal(
    publish(api, plan, artifact(), APP, { runId: "42" }).skipped,
    true,
  );
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

test("patch validator rejects traversal, workflow edits, credentials, symlinks and oversized data", () => {
  for (const path of [
    "../outside",
    "a/../../b",
    "/tmp/x",
    "a/.git/config",
    ".github/workflows/pwn.yml",
    ".claude/skills/x",
    ".env",
    "x/auth.json",
    "a\\b",
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
  assert.equal(stateFrom([{ id: 100, user: bot, body }], APP).task, state.task);
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
