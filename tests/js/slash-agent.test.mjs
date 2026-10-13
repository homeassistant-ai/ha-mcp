import test from "node:test";
import assert from "node:assert/strict";
import { command, checksReady, decide, renderState, stateFrom } from "../../.github/slash-agent/core.mjs";
import { collect, eventTarget, sessionRoot, snapshotDifferences } from "../../.github/slash-agent/github.mjs";
import { prepare, prompt } from "../../.github/slash-agent/main.mjs";
import { publish } from "../../.github/slash-agent/publish.mjs";
import { A, B, APP, user, bot, artifact, FakeAPI, initial, start, green, readySession } from "./slash-agent-fixtures.mjs";

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

test("a failure appearing during the worker remains unhandled for the next wakeup", () => {
  const api = readySession();
  api.checks[0].conclusion = "failure";
  const first = prepare(api, { number: 10, automatic: true }, APP);
  api.checks[0].id += 1;
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  publish(api, first, work, APP, { runId: "44" });
  assert.equal(prepare(api, { number: 10, automatic: true }, APP).decision.mode, "code");
});

test("only anchored, nonempty slash commands select supported models", () => {
  assert.equal(command("/astra implement this").model, "gpt-6.1-sol");
  assert.equal(command("/sol fix it\r\nkeep scope").model, "gpt-6.1-sol");
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
  snapshot.feedback.push({ id: 999, body: "An unhandled finding", author: "maintainer" });
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

test("existing checkpoints remain readable while new commands use the current model", () => {
  const api = new FakeAPI();
  const state = start(api);
  for (const model of ["gpt-5.6-sol", "gpt-6-sol", "gpt-6-astra"]) {
    state.model = model;
    api.comments.find((comment) => comment.id === 100).body = renderState(state, api.repository);
    assert.equal(stateFrom(api.edits(api.comments), APP).model, model);
  }
  api.command.body = "/sol resume";
  api.command.updated_at = "2026-09-15T21:00:00Z";
  const plan = initial(api);
  assert.equal(plan.decision.parsed.model, command(api.command.body).model);
});

test("stale work, repo/App mismatch and protected branches cannot publish worker output", () => {
  const api = new FakeAPI();
  const plan = initial(api);
  api.issue.body += " New scope";
  const skipped = publish(api, plan, artifact(), APP, { runId: "42" });
  assert.equal(skipped.skipped, true);
  assert.deepEqual(skipped.changed, ["issue"]);
  assert.deepEqual(snapshotDifferences(plan.snapshot, collect(api, 9, APP)), ["issue", "session"]);
  assert.ok(api.calls.every((call) => call.path === "issues/9/comments"));
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
