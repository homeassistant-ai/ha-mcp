import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCloseWorkflow } from "./issue-intake-helpers.mjs";
import {
  GitHub,
  main,
  makeContext,
  validateResult,
  render,
  prepare,
  labelAction,
  prose,
  publish,
  roleFor,
} from "../../.github/issue-intake/intake.mjs";

const bot = "ha-mcp[bot]";
function fixture() {
  return {
    repository: "test/repo",
    issue: {
      number: 1,
      title: "Report",
      body: "Client is Claude Desktop.\r\nVersion 8.4.3.",
      user: { login: "reporter", type: "User" },
      html_url: "https://github.com/test/repo/issues/1",
      state: "open",
      locked: false,
      labels: [],
    },
    roles: { reporter: "read", maintainer: "maintain" },
    events: [],
    comments: [
      {
        id: 1,
        user: { login: "maintainer", type: "User" },
        body: "Please provide the installation method.",
        html_url: "https://github.com/test/repo/issues/1#issuecomment-1",
        created_at: "2026-09-14T01:00:00Z",
      },
    ],
  };
}
function answer() {
  return {
    needs_translation: false,
    summary: [
      {
        text: "The reporter uses Claude Desktop.",
        evidence: [{ source_id: "body", quote: "Client is Claude Desktop." }],
      },
    ],
    translation: [],
    agreed_scope: [],
    facts: [
      {
        field: "client",
        value: "Claude Desktop",
        evidence: [{ source_id: "body", quote: "Client is Claude Desktop." }],
      },
    ],
    missing_fields: ["install_method"],
    already_requested: [
      {
        field: "install_method",
        evidence: [
          {
            source_id: "comment-1",
            quote: "Please provide the installation method.",
          },
        ],
      },
    ],
  };
}
test("requested questions remain missing, require maintainer evidence, and retain needs-info", () => {
  const s = fixture(),
    r = answer();
  assert.equal(labelAction(s, bot, validateResult(r, makeContext(s))), "add");
  assert.match(render(r, prepare(s, bot)), /maintainer has already requested/);
  assert.doesNotMatch(render(r, prepare(s, bot)), /How is ha-mcp installed/);
  r.missing_fields = [];
  assert.throws(
    () => validateResult(r, makeContext(s)),
    /already_requested\[0\].*missing_fields/,
  );
  r.missing_fields = ["install_method"];
  s.roles.maintainer = "write";
  assert.throws(
    () => validateResult(r, makeContext(s)),
    /already_requested\[0\].*maintainer/,
  );
  s.roles.maintainer = "maintain";
  r.already_requested[0].evidence[0].quote = "Invented maintainer request";
  assert.throws(
    () => validateResult(r, makeContext(s)),
    /already_requested\[0\].evidence\[0\]/,
  );
  r.already_requested[0].evidence[0].quote =
    "Please provide the installation method.";
  r.already_requested.push(structuredClone(r.already_requested[0]));
  assert.throws(
    () => validateResult(r, makeContext(s)),
    /already_requested\[1\].*duplicate field/,
  );
});
test("CRLF evidence matches LF without weakening other quote checks", () => {
  const s = fixture(),
    r = answer();
  r.summary[0].evidence[0].quote = "Client is Claude Desktop.\nVersion 8.4.3.";
  validateResult(r, makeContext(s));
  r.summary[0].evidence[0].quote = "Invented";
  assert.throws(
    () => validateResult(r, makeContext(s)),
    /summary\[0\].evidence\[0\].*body/,
  );
  const carriage = fixture(),
    carriageResult = answer();
  carriage.issue.body = "Client is Claude Desktop.\rVersion 8.4.3.";
  carriageResult.summary[0].evidence[0].quote =
    "Client is Claude Desktop.\nVersion 8.4.3.";
  validateResult(carriageResult, makeContext(carriage));
});
test("fact values must occur in their evidence", () => {
  const r = answer();
  r.facts[0].value = "Firefox";
  assert.throws(
    () => validateResult(r, makeContext(fixture())),
    /facts\[0\].value/,
  );
});

test("cross-field validation errors identify the affected item", () => {
  let r = answer();
  r.agreed_scope = [
    {
      text: "The reporter proposed a scope.",
      evidence: [{ source_id: "body", quote: "Client is Claude Desktop." }],
    },
  ];
  assert.throws(
    () => validateResult(r, makeContext(fixture())),
    /agreed_scope\[0\]/,
  );
  r = answer();
  r.missing_fields.push("client");
  assert.throws(
    () => validateResult(r, makeContext(fixture())),
    /facts\[0\]\.field.*missing_fields\[1\]/,
  );
});

test("an expired needs-info issue with a deleted reporter still has a close path", async () => {
  const writes = [],
    issue = { ...fixture().issue, user: null };
  const github = {
    rest: {
      issues: {
        listForRepo: "issues",
        listEvents: "events",
        listComments: "comments",
        createComment: async () => writes.push("comment"),
        update: async () => writes.push("close"),
        removeLabel: async () => writes.push("remove"),
      },
    },
    paginate: async (kind) =>
      kind === "issues"
        ? [issue]
        : kind === "comments"
          ? [
              {
                user: null,
                body: "Comment from a deleted account",
                created_at: "2026-01-02T00:00:00Z",
              },
            ]
          : [
              {
                id: 1,
                event: "labeled",
                actor: null,
                label: { name: "needs-info" },
                created_at: "2020-01-01T00:00:00Z",
              },
            ],
  };
  await runCloseWorkflow(github);
  assert.deepEqual(writes, ["comment", "close", "remove"]);
});
test("deleted comment authors do not interrupt reminders or later issues", async () => {
  const writes = [];
  const github = {
    rest: {
      issues: {
        listForRepo: "issues",
        listEvents: "events",
        listComments: "comments",
        createComment: async (args) => writes.push(args),
        update: async () => assert.fail("No closure before day seven"),
        removeLabel: async () => assert.fail("No reporter reply was supplied"),
      },
    },
    paginate: async (kind, args) => {
      if (kind === "issues")
        return [1, 2].map((number) => ({ ...fixture().issue, number }));
      if (kind === "events")
        return [
          {
            event: "labeled",
            label: { name: "needs-info" },
            created_at: new Date(Date.now() - 3.5 * 86400000).toISOString(),
          },
        ];
      return args.issue_number === 1
        ? [
            {
              user: null,
              created_at: new Date().toISOString(),
              body: "<!-- needs-info-warning:3 -->",
            },
          ]
        : [];
    },
  };
  await runCloseWorkflow(github);
  assert.deepEqual(
    writes.map((args) => args.issue_number),
    [1, 2],
  );
  assert.ok(
    writes.every((args) => args.body.includes("<!-- needs-info-warning:3 -->")),
  );
});

test("failed notices, reminders, and reply cleanup fail after the whole batch", async () => {
  const attempted = [];
  const now = Date.now();
  const github = {
    rest: {
      issues: {
        listForRepo: "issues",
        listEvents: "events",
        listComments: "comments",
        createComment: async ({ issue_number }) => {
          attempted.push(`comment-${issue_number}`);
          throw Error("Comment unavailable");
        },
        update: async () =>
          assert.fail("A failed closing notice must not close"),
        removeLabel: async ({ issue_number }) => {
          attempted.push(`remove-${issue_number}`);
          throw Object.assign(Error("Label unavailable"), { status: 500 });
        },
      },
    },
    paginate: async (kind, args) => {
      if (kind === "issues")
        return [1, 2, 3].map((number) => ({
          ...fixture().issue,
          number,
        }));
      if (kind === "events") {
        const age = args.issue_number === 1 ? 8 : 3.5;
        return [
          {
            event: "labeled",
            label: { name: "needs-info" },
            created_at: new Date(now - age * 86400000).toISOString(),
          },
        ];
      }
      return args.issue_number === 3
        ? [
            {
              user: { login: "reporter", type: "User" },
              created_at: new Date(now).toISOString(),
              body: "More details",
            },
          ]
        : [];
    },
  };
  await assert.rejects(runCloseWorkflow(github), (error) => {
    assert.match(error.message, /closing notices.*#1/i);
    assert.match(error.message, /reminders.*#2@day-3/i);
    assert.match(error.message, /reporter replies.*#3/i);
    return true;
  });
  assert.deepEqual(attempted, ["comment-1", "comment-2", "remove-3"]);
});
test("read failures are reported after later issues and prior write failures are preserved", async () => {
  const attempted = [],
    warnings = [],
    now = Date.now();
  const github = {
    rest: {
      issues: {
        listForRepo: "issues",
        listEvents: "events",
        listComments: "comments",
        createComment: async ({ issue_number }) => {
          attempted.push(issue_number);
          if (issue_number === 1) throw Error("Reminder unavailable");
        },
        update: async () => assert.fail("No issue is old enough to close"),
        removeLabel: async () => assert.fail("No reporter replied"),
      },
    },
    paginate: async (kind, args) => {
      if (kind === "issues")
        return [1, 2, 3, 4].map((number) => ({
          ...fixture().issue,
          number,
        }));
      if (kind === "events") {
        if (args.issue_number === 2)
          throw Object.assign(Error("Events unavailable"), { status: 502 });
        return [
          {
            event: "labeled",
            label: { name: "needs-info" },
            created_at: new Date(now - 3.5 * 86400000).toISOString(),
          },
        ];
      }
      if (args.issue_number === 3)
        throw Object.assign(Error("Comments unavailable"), { status: 502 });
      return [];
    },
  };
  await assert.rejects(
    runCloseWorkflow(github, { warning: (message) => warnings.push(message) }),
    (error) => {
      assert.match(error.message, /read issue data.*#2 \(events\).*#3 \(comments\)/i);
      assert.match(error.message, /reminders.*#1@day-3/i);
      return true;
    },
  );
  assert.deepEqual(attempted, [1, 4]);
  assert.ok(warnings.some((message) => /events.*#2/i.test(message)));
  assert.ok(warnings.some((message) => /comments.*#3/i.test(message)));
});

test("already-removed needs-info labels do not fail reply or close cleanup", async () => {
  const closed = [],
    now = Date.now();
  const github = {
    rest: {
      issues: {
        listForRepo: "issues",
        listEvents: "events",
        listComments: "comments",
        createComment: async () => {},
        update: async ({ issue_number }) => closed.push(issue_number),
        removeLabel: async () => {
          throw Object.assign(Error("Label already absent"), { status: 404 });
        },
      },
    },
    paginate: async (kind, args) => {
      if (kind === "issues")
        return [1, 2].map((number) => ({ ...fixture().issue, number }));
      if (kind === "events")
        return [
          {
            event: "labeled",
            label: { name: "needs-info" },
            created_at: new Date(now - 8 * 86400000).toISOString(),
          },
        ];
      return args.issue_number === 1
        ? [
            {
              user: { login: "reporter", type: "User" },
              created_at: new Date(now).toISOString(),
              body: "More details",
            },
          ]
        : [];
    },
  };
  await runCloseWorkflow(github);
  assert.deepEqual(closed, [2]);
});
test("translation requirement is a schema-enforced boolean", () => {
  const r = answer();
  r.needs_translation = "English (US)";
  assert.throws(
    () => validateResult(r, makeContext(fixture())),
    /needs_translation/,
  );
  r.needs_translation = false;
  validateResult(r, makeContext(fixture()));
});
test("mentions survive as display text without a broken numeric entity", () => {
  assert.equal(
    prose("ping @alice on #2404"),
    "ping @\u200balice on \\#\u200b2404",
  );
});
test("reporter replies clear needs-info for bot and triager labels before the close deadline", async () => {
  for (const actor of [
    { login: bot, type: "Bot" },
    { login: "triager", type: "User" },
  ]) {
    const writes = [];
    const github = {
      rest: {
        issues: {
          listForRepo: "issues",
          listEvents: "events",
          listComments: "comments",
          removeLabel: async () => writes.push("remove"),
          createComment: async () => writes.push("comment"),
          update: async () => writes.push("close"),
        },
      },
      paginate: async (kind) =>
        kind === "issues"
          ? [fixture().issue]
          : kind === "events"
            ? [
                {
                  id: 1,
                  event: "labeled",
                  actor,
                  label: { name: "needs-info" },
                  created_at: "2026-01-01T00:00:00Z",
                },
              ]
            : [
                {
                  user: { login: "reporter", type: "User" },
                  body: "Here are the details.",
                  created_at: "2026-01-02T00:00:00Z",
                },
              ],
    };
    await runCloseWorkflow(github);
    assert.deepEqual(writes, ["remove"]);
  }
});
test("API errors identify their endpoint without echoing secret-bearing stderr", () => {
  const api = new GitHub(() => {
    throw Object.assign(Error("secret"), { stderr: "HTTP 403 secret" });
  });
  assert.throws(
    () => api.request("repos/test/repo/issues/1"),
    (error) => {
      assert.match(error.message, /GET repos\/test\/repo\/issues\/1.*403/);
      assert.doesNotMatch(error.message, /secret/);
      return true;
    },
  );
});

test("role lookups ignore removed collaborators but propagate service failures", async () => {
  const api = {
    request: async () => {
      throw Object.assign(Error("unavailable"), { status: 503 });
    },
  };
  await assert.rejects(roleFor(api, "test/repo", "maintainer"), /unavailable/);
  api.request = async () => {
    throw Object.assign(Error("not found"), { status: 404 });
  };
  assert.equal(await roleFor(api, "test/repo", "removed"), "none");
});

test("malformed GitHub JSON identifies the endpoint without echoing its body", () => {
  const api = new GitHub(() => '{"body":"secret issue text",BROKEN}');
  assert.throws(
    () => api.request("repos/test/repo/issues/1"),
    (error) => {
      assert.match(
        error.message,
        /GET repos\/test\/repo\/issues\/1.*invalid JSON/,
      );
      assert.doesNotMatch(error.message, /secret|BROKEN/);
      return true;
    },
  );
});

function runtime(event, roles = {}) {
  const directory = mkdtempSync(join(tmpdir(), "intake-main-"));
  const env = {
    HA_MCP_APP_SLUG: "ha-mcp",
    GITHUB_REPOSITORY: "test/repo",
    GITHUB_EVENT_NAME: "issue_comment",
    GITHUB_ACTOR: "reporter",
    GITHUB_TRIGGERING_ACTOR: "reporter",
    GITHUB_EVENT_PATH: join(directory, "event.json"),
    GITHUB_OUTPUT: join(directory, "output"),
  };
  writeFileSync(env.GITHUB_EVENT_PATH, JSON.stringify(event));
  const calls = [];
  const api = {
    request(path) {
      calls.push(path);
      if (path.includes("/collaborators/"))
        return { role_name: roles[path.split("/").at(-2)] ?? "read" };
      throw Error("Unexpected request");
    },
  };
  return {
    directory,
    env,
    api,
    calls,
    cleanup: () => rmSync(directory, { recursive: true, force: true }),
  };
}
test("main checks both dispatch actors and does not admit write-only rerunners", async () => {
  const r = runtime(
    { sender: { type: "User" }, inputs: { issue_number: 1 } },
    { owner: "admin", writer: "write" },
  );
  try {
    r.env.GITHUB_EVENT_NAME = "workflow_dispatch";
    r.env.GITHUB_ACTOR = "owner";
    r.env.GITHUB_TRIGGERING_ACTOR = "writer";
    await assert.rejects(main("admit", r), /Maintainer dispatch/);
    assert.equal(r.calls.length, 2);
  } finally {
    r.cleanup();
  }
});
test("ordinary and unauthorized-command comments are admitted without permission lookups", async () => {
  for (const comment of [
    {
      body: "More details",
      user: { login: "reporter", type: "User" },
    },
    {
      body: "/triage refresh",
      user: { login: "reporter", type: "User" },
    },
    { body: "/triage pause", user: null },
  ]) {
    const r = runtime({
      sender: { type: "User" },
      issue: { number: 1 },
      comment,
    });
    try {
      await main("admit", r);
      assert.match(readFileSync(r.env.GITHUB_OUTPUT, "utf8"), /run=true/);
      assert.equal(r.calls.length, 0);
    } finally {
      r.cleanup();
    }
  }
});

test("bot comments are ignored even when the event sender is human", async () => {
  const r = runtime({
    sender: { type: "User" },
    issue: { number: 1 },
    comment: {
      body: "Bot update",
      user: { login: "automation[bot]", type: "Bot" },
    },
  });
  try {
    await main("admit", r);
    assert.match(readFileSync(r.env.GITHUB_OUTPUT, "utf8"), /run=false/);
    assert.equal(r.calls.length, 0);
  } finally {
    r.cleanup();
  }
});

test("main admits a maintainer refresh and filters bot or PR events", async () => {
  for (const [extra, expected] of [
    [{}, true],
    [{ issue: { number: 1, pull_request: {} } }, false],
    [{ sender: { type: "Bot" } }, false],
  ]) {
    const r = runtime(
      {
        sender: { type: "User" },
        issue: { number: 1 },
        comment: {
          body: "/triage refresh",
          user: { login: "maintainer", type: "User" },
        },
        ...extra,
      },
      { maintainer: "maintain" },
    );
    try {
      await main("admit", r);
      assert.match(
        readFileSync(r.env.GITHUB_OUTPUT, "utf8"),
        new RegExp(`run=${expected}`),
      );
      assert.equal(r.calls.length, 0);
    } finally {
      r.cleanup();
    }
  }
});

test("dispatch maps removed collaborators to none", async () => {
  const r = runtime({ sender: { type: "User" }, inputs: { issue_number: 1 } });
  r.env.GITHUB_EVENT_NAME = "workflow_dispatch";
  r.env.GITHUB_ACTOR = "removed";
  r.env.GITHUB_TRIGGERING_ACTOR = "removed";
  r.api.request = () => {
    throw Object.assign(Error("not found"), { status: 404 });
  };
  try {
    await assert.rejects(main("admit", r), /Maintainer dispatch required/);
  } finally {
    r.cleanup();
  }
});

test("unauthorized triage commands cannot change context or its fingerprint", () => {
  const s = fixture();
  const before = prepare(s, bot);
  s.comments.push({
    id: 2,
    user: { login: "reporter", type: "User" },
    body: "The installation uses HACS.",
    html_url: "https://github.com/test/repo/issues/1#issuecomment-2",
    created_at: "2026-09-14T01:30:00Z",
  });
  const updated = prepare(s, bot);
  assert.notEqual(updated.digest, before.digest);
  s.comments.push({
    id: 3,
    user: { login: "reporter", type: "User" },
    body: "/triage pause",
    html_url: "https://github.com/test/repo/issues/1#issuecomment-3",
    created_at: "2026-09-14T02:00:00Z",
  });
  assert.deepEqual(makeContext(s), updated.context);
  assert.equal(prepare(s, bot).digest, updated.digest);
});
test("main rejects mismatched App and repository before any publication API call", async () => {
  const r = runtime({});
  try {
    r.env.TOKEN_APP_SLUG = "different";
    await assert.rejects(main("publish", r), /different App/);
    r.env.TOKEN_APP_SLUG = "ha-mcp";
    writeFileSync(
      join(r.directory, "prepared.json"),
      JSON.stringify({ snapshot: { repository: "other/repo" } }),
    );
    await assert.rejects(main("publish", r), /repository mismatch/);
    assert.equal(r.calls.length, 0);
  } finally {
    r.cleanup();
  }
});

test("main collection writes the new schema and prepared human context", async () => {
  const r = runtime({
    sender: { type: "User" },
    issue: { number: 1 },
    comment: {
      body: "More details",
      user: { login: "reporter", type: "User" },
    },
  });
  const s = fixture();
  r.api.request = (path) =>
    path.includes("/collaborators/")
      ? { role_name: s.roles[path.split("/").at(-2)] }
      : path.includes("/comments?")
        ? s.comments
        : path.includes("/events?")
          ? s.events
          : s.issue;
  try {
    await main("collect", r);
    assert.match(readFileSync(r.env.GITHUB_OUTPUT, "utf8"), /run=true/);
    assert.equal(
      JSON.parse(readFileSync(join(r.directory, "schema.json"), "utf8"))
        .properties.needs_translation.type,
      "boolean",
    );
    assert.equal(
      JSON.parse(readFileSync(join(r.directory, "prepared.json"), "utf8"))
        .context.sources.length,
      2,
    );
  } finally {
    r.cleanup();
  }
});

test("a mid-run lock skips all publication writes and emits a workflow warning", async () => {
  const s = fixture(),
    prepared = prepare(s, bot),
    messages = [];
  s.issue.locked = true;
  const api = {
    request(path, options = {}) {
      assert.ok(!options.method || options.method === "GET");
      return path.includes("/collaborators/")
        ? { role_name: s.roles[path.split("/").at(-2)] }
        : path.includes("/comments?")
          ? s.comments
          : path.includes("/events?")
            ? s.events
            : s.issue;
    },
  };
  const originalLog = console.log;
  try {
    console.log = (message) => messages.push(message);
    assert.match(await publish(api, prepared, answer(), bot), /skipped/);
  } finally {
    console.log = originalLog;
  }
  assert.ok(messages.some((message) => message.startsWith("::warning::")));
});
