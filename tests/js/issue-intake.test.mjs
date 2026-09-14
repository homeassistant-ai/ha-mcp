import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  collect,
  control,
  fingerprint,
  makeContext,
  prepare,
  publish,
  render,
  validateResult,
  marker,
} from "../../.github/issue-intake/intake.mjs";

const bot = "ha-mcp[bot]";

test("one fact category can contain multiple explicitly reported values", () => {
  const s = snapshot();
  s.issue.body += " Also reproduced with ChatGPT.";
  const r = result();
  r.facts.push({
    field: "client",
    value: "ChatGPT",
    evidence: [{ source_id: "body", quote: "Also reproduced with ChatGPT." }],
  });
  const text = render(r, prepare(s, bot));
  assert.match(text, /Client: Claude Desktop/);
  assert.match(text, /Client: ChatGPT/);
});

test("validated environment facts are visible with source citations", () => {
  const text = render(result(), prepare(snapshot(), bot));
  assert.match(text, /### Reported details/);
  assert.match(text, /Client: Claude Desktop \[source\]/);
});

test("temporary label and final-patch failures recover without duplicate comments", async () => {
  for (const stage of ["label", "final"]) {
    const s = snapshot(),
      api = new FakeGitHub(s);
    const request = api.request.bind(api);
    let failed = false;
    api.request = (path, options = {}) => {
      const target =
        stage === "label"
          ? path.endsWith("/labels")
          : options.method === "PATCH" &&
            !options.data.body.includes(" pending -->");
      if (target && !failed) {
        failed = true;
        throw Object.assign(Error("Transient failure"), { status: 503 });
      }
      return request(path, options);
    };
    await publish(api, prepare(s, bot), result(), bot);
    assert.equal(failed, true);
    assert.equal(api.data.comments.length, 1);
    assert.equal(api.data.issue.labels[0].name, "needs-info");
    assert.equal(prepare(await collect(api, "test/repo", 1), bot).run, false);
  }
});

test("exhausted transient writes stay pending for manual recovery", async () => {
  const s = snapshot(),
    api = new FakeGitHub(s);
  const request = api.request.bind(api);
  let attempts = 0;
  api.request = (path, options = {}) => {
    if (path.endsWith("/labels")) {
      attempts += 1;
      throw Object.assign(Error("Outage"), { status: 503 });
    }
    return request(path, options);
  };
  await assert.rejects(publish(api, prepare(s, bot), result(), bot), /Outage/);
  assert.equal(attempts, 3);
  assert.equal(api.data.comments.length, 1);
  assert.match(api.data.comments[0].body, / pending -->/);
  assert.equal(prepare(await collect(api, "test/repo", 1), bot).run, true);
});

test("a maintainer pause during backoff stops the retry", async () => {
  const s = snapshot(),
    api = new FakeGitHub(s);
  const request = api.request.bind(api);
  let attempts = 0;
  api.request = (path, options = {}) => {
    if (path.endsWith("/labels")) {
      attempts += 1;
      api.data.comments.push(comment(2, "maintainer", "/triage pause"));
      throw Object.assign(Error("Temporary failure"), { status: 503 });
    }
    return request(path, options);
  };
  assert.match(await publish(api, prepare(s, bot), result(), bot), /skipped/);
  assert.equal(attempts, 1);
  assert.equal(api.data.issue.labels.length, 0);
});

test("failed day-seven closes retain needs-info for the next daily run", async () => {
  const yaml = readFileSync(
    new URL("../../.github/workflows/close-needs-info.yml", import.meta.url),
    "utf8",
  );
  const code = yaml
    .split("script: |\n")[1]
    .split("\n")
    .map((line) => line.replace(/^ {12}/, ""))
    .join("\n");
  const removed = [],
    closed = [];
  const github = {
    rest: {
      issues: {
        listForRepo: "issues",
        listEvents: "events",
        listComments: "comments",
        createComment: async () => {},
        removeLabel: async (p) => removed.push(p.issue_number),
        update: async (p) => {
          if (p.issue_number === 1) throw Error("Temporary close error");
          closed.push(p.issue_number);
        },
      },
      repos: {
        getCollaboratorPermissionLevel: async () => ({
          data: { role_name: "maintain" },
        }),
      },
    },
    paginate: async (kind) =>
      kind === "issues"
        ? [1, 2].map((number) => ({
            number,
            title: "Fixture",
            user: user("reporter"),
          }))
        : kind === "comments"
          ? []
          : [
              {
                id: 1,
                event: "labeled",
                actor: user("maintainer"),
                label: { name: "needs-info" },
                created_at: "2020-01-01T00:00:00Z",
              },
            ],
  };
  await assert.rejects(
    new Function(
      "github",
      "context",
      "core",
      `return (async () => {${code}})()`,
    )(
      github,
      { repo: { owner: "test", repo: "repo" } },
      {
        info() {},
        warning() {},
        setFailed(message) {
          throw Error(message);
        },
      },
    ),
    /Failed to close/,
  );
  assert.deepEqual(closed, [2]);
  assert.deepEqual(removed, [2]);
});

test("deleted issue authors retain their report without maintainer authority", () => {
  const s = snapshot();
  s.issue.user = null;
  const context = makeContext(s);
  assert.equal(context.sources[0].author, "ghost");
  assert.equal(context.sources[0].maintainer, false);
});

test("bare model URLs are not autolinks but source citations remain usable", () => {
  const r = result();
  r.summary[0].text = "See https://evil.test/path and www.evil.test";
  const text = render(r, prepare(snapshot(), bot));
  assert.match(text, /https\[:\]\/\/evil\.test\/path/);
  assert.match(text, /www\[\.\]evil\.test/);
  assert.match(
    text,
    /\[source\]\(https:\/\/github\.com\/test\/repo\/issues\/1\)/,
  );
});

test("non-English reports require an English translation", () => {
  const r = result();
  r.language = "Italian";
  assert.throws(
    () => validateResult(r, makeContext(snapshot())),
    /translation/,
  );
});

test("a failed permission lookup does not skip later confirmed issues", async () => {
  const yaml = readFileSync(
    new URL("../../.github/workflows/close-needs-info.yml", import.meta.url),
    "utf8",
  );
  const code = yaml
    .split("script: |\n")[1]
    .split("\n")
    .map((line) => line.replace(/^ {12}/, ""))
    .join("\n");
  for (const status of [404, 500]) {
    const closed = [];
    const github = {
      rest: {
        issues: {
          listForRepo: "issues",
          listEvents: "events",
          listComments: "comments",
          createComment: async () => {},
          removeLabel: async () => {},
          update: async (p) => closed.push(p.issue_number),
        },
        repos: {
          getCollaboratorPermissionLevel: async (p) => {
            if (p.username === "removed")
              throw Object.assign(Error("Lookup failed"), { status });
            return { data: { role_name: "maintain" } };
          },
        },
      },
      paginate: async (kind, p) =>
        kind === "issues"
          ? [1, 2].map((number) => ({
              number,
              title: "Fixture",
              user: user("reporter"),
            }))
          : kind === "comments"
            ? []
            : [
                {
                  id: 1,
                  event: "labeled",
                  label: { name: "needs-info" },
                  actor: user(p.issue_number === 1 ? "removed" : "maintainer"),
                  created_at: "2020-01-01T00:00:00Z",
                },
              ],
    };
    const run = new Function(
      "github",
      "context",
      "core",
      `return (async () => {${code}})()`,
    )(
      github,
      { repo: { owner: "test", repo: "repo" } },
      {
        info() {},
        warning() {},
        setFailed(message) {
          throw Error(message);
        },
      },
    );
    if (status === 500) await assert.rejects(run, /permission/i);
    else await run;
    assert.deepEqual(closed, [2]);
  }
});
const user = (login) => ({ login, type: "User" });
const comment = (id, login, body, extra = {}) => ({
  id,
  user: user(login),
  body,
  html_url: `https://github.com/test/repo/issues/1#issuecomment-${id}`,
  created_at: `2026-09-${String(id + 10).padStart(2, "0")}T00:00:00Z`,
  ...extra,
});
function snapshot() {
  return {
    repository: "test/repo",
    issue: {
      number: 1,
      title: "Dashboard call hangs",
      body: "Using Claude Desktop on Windows. The dashboard call hangs.",
      html_url: "https://github.com/test/repo/issues/1",
      user: user("reporter"),
      state: "open",
      locked: false,
      labels: [],
    },
    comments: [],
    events: [],
    roles: { reporter: "read", maintainer: "maintain", writer: "write" },
  };
}
function result() {
  return {
    language: "English",
    summary: [
      {
        text: "The reporter says the dashboard call hangs.",
        evidence: [{ source_id: "body", quote: "The dashboard call hangs." }],
      },
    ],
    translation: [],
    agreed_scope: [],
    facts: [
      {
        field: "client",
        value: "Claude Desktop",
        evidence: [{ source_id: "body", quote: "Using Claude Desktop" }],
      },
    ],
    missing_fields: ["ha_mcp_version", "install_method"],
    already_requested: [],
  };
}
class FakeGitHub {
  constructor(data) {
    this.data = structuredClone(data);
    this.writes = [];
    this.failLabel = false;
  }
  request(path, options = {}) {
    if (options.method && options.method !== "GET") {
      this.writes.push({ path, ...options });
      if (path.endsWith("/labels")) {
        if (this.failLabel) throw Error("Label write failed");
        this.data.issue.labels.push({ name: "needs-info" });
        this.data.events.push({
          id: 99,
          event: "labeled",
          label: { name: "needs-info" },
          actor: { login: bot, type: "Bot" },
          created_at: "2026-09-25T00:00:00Z",
        });
      } else if (path.endsWith("/labels/needs-info")) {
        this.data.issue.labels = [];
      } else if (path.endsWith("/comments")) {
        this.data.comments.push(
          comment(10, bot, options.data.body, {
            user: { login: bot, type: "Bot" },
          }),
        );
        return { id: 10 };
      } else if (path.includes("/comments/")) {
        this.data.comments.find(
          (c) => c.id === Number(path.split("/").at(-1)),
        ).body = options.data.body;
      }
      return {};
    }
    if (path.includes("/collaborators/"))
      return { role_name: this.data.roles[path.split("/").at(-2)] || "none" };
    if (path.includes("/comments?")) return structuredClone(this.data.comments);
    if (path.includes("/events?")) return structuredClone(this.data.events);
    return structuredClone(this.data.issue);
  }
}

test("human replies and maintainer authority survive; bot theories do not enter context", () => {
  const s = snapshot();
  s.comments.push(
    comment(1, "writer", "Proposal"),
    comment(2, "maintainer", "Approved"),
    comment(3, bot, "Invented root cause", {
      user: { login: bot, type: "Bot" },
    }),
    comment(4, "ghhamcp", "Automated diagnosis"),
  );
  const context = makeContext(s);
  assert.deepEqual(
    context.sources.map((x) => x.source_id),
    ["body", "comment-1", "comment-2"],
  );
  assert.equal(context.sources[1].maintainer, false);
  assert.equal(context.sources[2].maintainer, true);
  assert.match(context.sources[0].text, /Dashboard call hangs/);
});

test("only maintain/admin commands control pause; resume is durable", () => {
  const s = snapshot();
  s.comments.push(comment(1, "writer", "/triage pause"));
  assert.equal(control(s).paused, false);
  s.comments.push(comment(2, "maintainer", "/triage pause"));
  assert.equal(prepare(s, bot).run, false);
  s.comments.push(comment(3, "reporter", "/triage resume"));
  assert.equal(prepare(s, bot).run, false);
  s.comments.push(comment(4, "maintainer", "/triage resume"));
  assert.equal(prepare(s, bot).run, true);
});

test("edited maintainer control is ordered by edit time, not original position", () => {
  const s = snapshot();
  s.comments.push(
    comment(1, "maintainer", "/triage pause", {
      updated_at: "2026-09-28T00:00:00Z",
    }),
    comment(2, "maintainer", "/triage resume"),
  );
  assert.equal(control(s).paused, true);
});

test("unverified quotes, fields, properties, and approvals are rejected", () => {
  const s = snapshot();
  s.comments.push(comment(1, "writer", "I approve this scope."));
  for (const mutate of [
    (r) => {
      r.summary[0].evidence[0].quote = "not in the issue";
    },
    (r) => {
      r.summary[0].evidence[0].source_id = "comment-999";
    },
    (r) => {
      r.missing_fields.push("client");
    },
    (r) => {
      r.missing_fields.push("root_cause");
    },
    (r) => {
      r.root_cause = "invented";
    },
    (r) => {
      r.toString = "unexpected inherited property";
    },
    (r) => {
      r.agreed_scope = [
        {
          text: "Approved",
          evidence: [{ source_id: "comment-1", quote: "I approve" }],
        },
      ];
    },
  ]) {
    const r = result();
    mutate(r);
    assert.throws(() => validateResult(r, makeContext(s)));
  }
});

test("human-approved scope can cite the contributor proposal and maintainer approval", () => {
  const s = snapshot();
  s.comments.push(
    comment(1, "writer", "Automation only."),
    comment(2, "maintainer", "I agree."),
  );
  const r = result();
  r.agreed_scope = [
    {
      text: "The maintainer agreed to automation only.",
      evidence: [
        { source_id: "comment-1", quote: "Automation only." },
        { source_id: "comment-2", quote: "I agree." },
      ],
    },
  ];
  assert.match(render(r, prepare(s, bot)), /Scope agreed/);
});

test("render neutralizes model mentions and links and avoids repeating questions", () => {
  const r = result();
  r.summary[0].text =
    "@maintainer ![track](https://evil.test) <script>payload</script>";
  r.already_requested = ["install_method"];
  const text = render(r, prepare(snapshot(), bot));
  assert.ok(!text.includes("@maintainer"));
  assert.ok(!text.includes("![track]"));
  assert.ok(!text.includes("<script>"));
  assert.ok(!text.includes("How is ha-mcp installed"));
  assert.match(text, /Which ha-mcp version/);
});

test("publication is idempotent and revises the same comment after a human answer", async () => {
  const s = snapshot(),
    api = new FakeGitHub(s);
  await publish(api, prepare(s, bot), result(), bot);
  assert.equal(api.data.comments.length, 1);
  assert.equal(api.data.issue.labels[0].name, "needs-info");
  assert.equal(prepare(await collect(api, "test/repo", 1), bot).run, false);
  api.data.comments.push(
    comment(2, "reporter", "Version 8.4.3; HACS integration."),
  );
  const fresh = await collect(api, "test/repo", 1);
  const r = result();
  r.missing_fields = [];
  await publish(api, prepare(fresh, bot), r, bot);
  assert.equal(api.data.comments.filter((c) => c.user.login === bot).length, 1);
  assert.deepEqual(api.data.issue.labels, []);
  assert.equal(prepare(await collect(api, "test/repo", 1), bot).run, false);
});

test("label failure leaves a retryable checkpoint and does not create a duplicate comment", async () => {
  const s = snapshot(),
    api = new FakeGitHub(s);
  api.failLabel = true;
  await assert.rejects(
    publish(api, prepare(s, bot), result(), bot),
    /Label write failed/,
  );
  assert.match(api.data.comments[0].body, /pending -->/);
  const fresh = await collect(api, "test/repo", 1);
  assert.equal(prepare(fresh, bot).run, true);
  api.failLabel = false;
  await publish(api, prepare(fresh, bot), result(), bot);
  assert.equal(api.data.comments.length, 1);
  assert.equal(prepare(await collect(api, "test/repo", 1), bot).run, false);
});

test("a new reply, closure, or maintainer pause during inference prevents stale publication", async () => {
  for (const mutate of [
    (s) => s.comments.push(comment(2, "reporter", "Correction: it works now.")),
    (s) => {
      s.issue.state = "closed";
    },
    (s) => s.comments.push(comment(2, "maintainer", "/triage pause")),
  ]) {
    const s = snapshot(),
      api = new FakeGitHub(s);
    mutate(api.data);
    assert.match(await publish(api, prepare(s, bot), result(), bot), /skipped/);
    assert.equal(api.writes.length, 0);
  }
});

test("manual label removal persists until explicit maintainer resume", async () => {
  const s = snapshot();
  s.events.push({
    id: 1,
    event: "unlabeled",
    actor: user("maintainer"),
    label: { name: "needs-info" },
    created_at: "2026-09-10T00:00:00Z",
  });
  const api = new FakeGitHub(s);
  await publish(api, prepare(s, bot), result(), bot);
  assert.equal(api.writes.filter((w) => w.path.endsWith("/labels")).length, 0);
  api.data.comments.push(comment(2, "maintainer", "/triage resume"));
  await publish(
    api,
    prepare(await collect(api, "test/repo", 1), bot),
    result(),
    bot,
  );
  assert.equal(api.data.issue.labels[0].name, "needs-info");
});

test("the bot never removes a human-owned needs-info label", async () => {
  const s = snapshot();
  s.issue.labels = [{ name: "needs-info" }];
  s.events.push({
    id: 1,
    event: "labeled",
    actor: user("maintainer"),
    label: { name: "needs-info" },
    created_at: "2026-09-10T00:00:00Z",
  });
  const r = result();
  r.missing_fields = [];
  const api = new FakeGitHub(s);
  await publish(api, prepare(s, bot), r, bot);
  assert.deepEqual(api.data.issue.labels, [{ name: "needs-info" }]);
});

test("an unrelated bot cannot forge a completed documentation checkpoint", () => {
  const s = snapshot();
  s.comments.push(
    comment(2, "evil[bot]", `${marker}${fingerprint(s)} -->`, {
      user: { login: "evil[bot]", type: "Bot" },
    }),
  );
  assert.equal(prepare(s, bot).run, true);
});

test("collection rejects PRs and oversized context rather than dropping late replies", async () => {
  const s = snapshot();
  s.issue.pull_request = {};
  await assert.rejects(
    collect(new FakeGitHub(s), "test/repo", 1),
    /pull request/,
  );
  delete s.issue.pull_request;
  s.comments.push(comment(2, "reporter", "a".repeat(170000)));
  await assert.rejects(collect(new FakeGitHub(s), "test/repo", 1), /budget/);
});

test("real close workflow requires a human maintainer label; bot requests never close", async () => {
  const yaml = readFileSync(
    new URL("../../.github/workflows/close-needs-info.yml", import.meta.url),
    "utf8",
  );
  const code = yaml
    .split("script: |\n")[1]
    .split("\n")
    .map((line) => line.replace(/^ {12}/, ""))
    .join("\n");
  for (const [actor, role, closes] of [
    [{ login: bot, type: "Bot" }, "admin", false],
    [user("writer"), "write", false],
    [user("maintainer"), "maintain", true],
  ]) {
    const writes = [];
    const issue = { number: 1, title: "Example", user: user("reporter") };
    const endpoints = {
      listForRepo: "issues",
      listEvents: "events",
      listComments: "comments",
      createComment: async () => writes.push("comment"),
      removeLabel: async () => writes.push("remove"),
      update: async () => writes.push("close"),
    };
    const github = {
      rest: {
        issues: endpoints,
        repos: {
          getCollaboratorPermissionLevel: async () => ({
            data: { role_name: role },
          }),
        },
      },
      paginate: async (type) =>
        type === "issues"
          ? [issue]
          : type === "events"
            ? [
                {
                  id: 1,
                  event: "labeled",
                  actor,
                  label: { name: "needs-info" },
                  created_at: "2020-01-01T00:00:00Z",
                },
              ]
            : [],
    };
    await new Function(
      "github",
      "context",
      "core",
      `return (async () => {${code}})()`,
    )(
      github,
      { repo: { owner: "test", repo: "repo" } },
      {
        info() {},
        warning() {},
        setFailed(message) {
          throw Error(message);
        },
      },
    );
    assert.equal(writes.includes("close"), closes);
    if (!closes) assert.equal(writes.length, 0);
  }
});
