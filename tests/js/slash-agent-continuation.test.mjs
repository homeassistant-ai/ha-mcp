import test from "node:test";
import assert from "node:assert/strict";
import { command, stateFrom } from "../../.github/slash-agent/core.mjs";
import { prepare } from "../../.github/slash-agent/main.mjs";
import { publish } from "../../.github/slash-agent/publish.mjs";
import { APP, user, artifact, FakeAPI, start, green } from "./slash-agent-fixtures.mjs";

const wake = (api) => prepare(api, { number: 10, automatic: true }, APP);
const unchanged = () => {
  const work = artifact();
  work.changes = [];
  work.result.outcome = "unchanged";
  return work;
};
const checkpoint = (api) => stateFrom(api.edits(api.comments), APP);

for (const removal of ["resolve", "delete", "dismiss"]) {
  test(`removing handled feedback (${removal}) cannot buy another turn`, () => {
    const api = new FakeAPI();
    start(api);
    green(api);
    if (removal === "dismiss") {
      api.reviews = [{ id: 500, user, body: "Explain the approach", state: "CHANGES_REQUESTED", submitted_at: "2026-09-15T13:00:00Z" }];
    } else {
      api.reviewThreads = [{ id: "review-1", isResolved: false, comments: [{ id: 500, user, body: "Explain the approach", updated_at: "2026-09-15T13:00:00Z" }] }];
      if (removal === "delete") api.reviewThreads[0].comments.push({ id: 502, user, body: "Keep this handled comment", updated_at: "2026-09-15T13:00:00Z" });
    }
    publish(api, wake(api), unchanged(), APP, { runId: "44" });
    const rounds = checkpoint(api).rounds;
    if (removal === "dismiss") api.reviews[0].state = "DISMISSED";
    else if (removal === "resolve") api.reviewThreads[0].isResolved = true;
    else api.reviewThreads[0].comments.splice(0, 1);
    const plan = wake(api);
    assert.notEqual(plan?.decision.mode, "code");
    if (plan) publish(api, plan, null, APP, { runId: "45" });
    assert.equal(checkpoint(api).rounds, rounds);
    api.reviews.push({ id: 501, user, body: "A new finding", state: "COMMENTED", submitted_at: "2026-09-15T14:00:00Z" });
    assert.equal(wake(api).decision.mode, "code");
  });
}

for (const body of ["/sol\nnot a command", `/sol ${"x".repeat(12001)}`]) {
  test("invalid slash-looking comments cannot supersede publication", () => {
    const api = new FakeAPI();
    start(api);
    api.comments.push({ id: 501, user, body, updated_at: "2026-09-15T14:00:00Z" });
    api.checks = [{ id: 99, name: "Unit Tests", app: { id: 15368 }, status: "completed", conclusion: "failure" }];
    const plan = wake(api);
    assert.equal(plan.decision.mode, "code");
    assert.doesNotThrow(() => publish(api, plan, unchanged(), APP, { runId: "44" }));
    assert.equal(checkpoint(api).rounds, 2);
    assert.equal(wake(api), null);
  });
}

for (const closed of ["issue", "pr"]) {
  test(`a fresh command after ${closed} closure gets a visible refusal without model work`, () => {
    const api = new FakeAPI();
    start(api);
    api[closed].state = "closed";
    api.prComments.push({ id: 501, user, body: "/sol take a different approach", updated_at: "2026-09-15T14:00:00Z" });
    const plan = prepare(api, { number: 10, commandId: 501, automatic: false }, APP);
    assert.ok(plan);
    assert.notEqual(plan.decision.mode, "code");
    const state = publish(api, plan, null, APP, { runId: "44" });
    assert.match(state.summary, /closed|merged/i);
    assert.equal(state.rounds, 1);
    assert.equal(wake(api), null);
    assert.equal(prepare(api, { number: 10, commandId: 501, automatic: false }, APP), null);
    assert.equal(api.pr.state, closed === "pr" ? "closed" : "open");
  });
}

test("a note above the managed description survives while its summary updates", () => {
  const api = new FakeAPI();
  start(api);
  api.pr.body = `Maintainer note\n\n${api.pr.body}\n\nBot release notes`;
  api.reviews.push({ id: 501, user, body: "Clarify the summary", state: "COMMENTED", submitted_at: "2026-09-15T14:00:00Z" });
  const work = unchanged();
  work.result.summary = "Updated the explanation.";
  publish(api, wake(api), work, APP, { runId: "44" });
  assert.ok(api.pr.body.startsWith("Maintainer note\n\n"));
  assert.ok(api.pr.body.endsWith("Bot release notes"));
  assert.match(api.pr.body, /Updated the explanation/);
  assert.ok(!api.pr.body.includes("Fixed the accepted scope"));
});

test("command admission bounds JSON escaping before a paid turn", () => {
  for (const escaped of ["\n", '"', "\\"]) {
    assert.equal(command(`/sol x${escaped.repeat(11998)}x`), null);
  }
  assert.ok(command(`/sol ${"x".repeat(12000)}`));
  assert.ok(command(`/sol ${"漢".repeat(4000)}`));
});

for (const concurrent of [false, true]) {
  test(`PR head caching after our append ${concurrent ? "cannot hide a concurrent branch update" : "does not interrupt replies or mark stale checks ready"}`, () => {
    const api = new FakeAPI();
    start(api);
    green(api);
    api.reviewThreads = [{ id: "review-1", isResolved: false, comments: [{ id: 500, user, body: "Add the regression", updated_at: "2026-09-15T13:00:00Z" }] }];
    const plan = wake(api);
    const oldHead = api.pr.head.sha;
    const nextHead = "c".repeat(40);
    const write = api.write.bind(api);
    api.write = (path, data, method) => {
      const result = write(path, data, method);
      if (path === "git/commits") return { sha: nextHead };
      if (path.startsWith("git/refs/heads/")) {
        api.pr.head.sha = oldHead;
        if (concurrent) api.branches[api.pr.head.ref] = "d".repeat(40);
      }
      return result;
    };
    const work = artifact();
    work.result.responses = [{ thread_id: "review-1", body: "Added the regression; focused tests passed.", resolve: true }];
    if (concurrent) {
      assert.throws(() => publish(api, plan, work, APP, { runId: "44" }), /changed during publication/);
      assert.equal(api.reviewThreads[0].isResolved, false);
      return;
    }
    const state = publish(api, plan, work, APP, { runId: "44" });
    assert.equal(state.lastHead, nextHead);
    assert.equal(state.status, "waiting");
    assert.equal(api.pr.draft, true);
    assert.equal(api.reviewThreads[0].isResolved, true);
    api.pr.head.sha = nextHead;
    api.checks = [];
    assert.equal(wake(api), null);
    green(api);
    assert.equal(wake(api).decision.mode, "ready");
  });
}

test("discarded paid work consumes its budget once even when public source changes repeatedly", () => {
  const api = new FakeAPI();
  for (let round = 1; round <= 4; round++) {
    const plan = prepare(api, { number: 9, commandId: 1, automatic: false }, APP);
    assert.equal(plan.decision.mode, "code");
    api.issue.body += ` Reporter edit ${round}`;
    const skipped = publish(api, plan, artifact(), APP, { runId: String(40 + round) });
    assert.equal(skipped.skipped, true);
    assert.equal(checkpoint(api).rounds, round);
    publish(api, plan, artifact(), APP, { runId: String(40 + round) });
    assert.equal(checkpoint(api).rounds, round);
    assert.equal(api.pr, null);
    assert.equal(api.calls.filter((c) => c.path === "git/commits").length, 0);
  }
  assert.equal(prepare(api, { number: 9, commandId: 1, automatic: false }, APP), null);
  assert.equal(checkpoint(api).status, "blocked");
  api.comments.push({ id: 501, user, body: "/sol a renewed task", updated_at: "2026-09-15T14:00:00Z" });
  const renewed = prepare(api, { number: 9, commandId: 501, automatic: false }, APP);
  api.issue.body += " Another reporter edit";
  publish(api, renewed, artifact(), APP, { runId: "50" });
  assert.equal(checkpoint(api).rounds, 1);
  assert.equal(prepare(api, { number: 9, automatic: true }, APP).decision.mode, "code");
});

test("stale-attempt accounting cannot overwrite a newer command or revoked authority", () => {
  for (const change of ["command", "role"]) {
    const api = new FakeAPI();
    const plan = prepare(api, { number: 9, commandId: 1, automatic: false }, APP);
    if (change === "command") api.comments.push({ id: 501, user, body: "/sol a different authorized task", updated_at: "2026-09-15T14:00:00Z" });
    else api.roles.maintainer = "write";
    assert.equal(publish(api, plan, artifact(), APP, { runId: "44" }).skipped, true);
    assert.equal(api.calls.length, 0);
  }
});

test("discarding a new task on a ready PR must retry its work before restoring readiness", () => {
  const api = new FakeAPI();
  start(api);
  green(api);
  publish(api, wake(api), null, APP, { runId: "43" });
  api.prComments.push({ id: 501, user, body: "/sol implement the next requested change", updated_at: "2026-09-15T14:00:00Z" });
  const plan = prepare(api, { number: 10, commandId: 501, automatic: false }, APP);
  api.issue.body += " Reporter edit during the new task";
  assert.equal(publish(api, plan, artifact(), APP, { runId: "44" }).skipped, true);
  assert.equal(wake(api).decision.mode, "code");
});
