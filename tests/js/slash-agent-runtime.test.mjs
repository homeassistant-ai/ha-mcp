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

import { A, B, APP, user, bot, response, artifact, FakeAPI, initial, start, green, readySession } from "./slash-agent-fixtures.mjs";

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
