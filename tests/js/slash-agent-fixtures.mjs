import assert from "node:assert/strict";
import { digest } from "../../.github/slash-agent/core.mjs";
import { prepare } from "../../.github/slash-agent/main.mjs";
import { publish } from "../../.github/slash-agent/publish.mjs";

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


export { A, B, APP, user, bot, response, artifact, FakeAPI, initial, start, green, readySession };
