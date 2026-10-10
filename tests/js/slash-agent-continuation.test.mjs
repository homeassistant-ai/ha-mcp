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
