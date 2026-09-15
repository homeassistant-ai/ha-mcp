import { GitHub } from "../issue-intake/intake.mjs";
import {
  command,
  digest,
  maintainer,
  ORIGIN_MARKER,
  REVIEW_BOTS,
  stateFrom,
  trustedReview,
} from "./core.mjs";

export class API extends GitHub {
  constructor(repository, options) {
    super(options);
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository))
      throw Error("Invalid repository");
    this.repository = repository;
    this.root = `repos/${repository}`;
  }
  get(path) {
    return this.request(`${this.root}/${path}`);
  }
  write(path, data, method = "POST") {
    return this.request(`${this.root}/${path}`, { method, data });
  }
  pages(path, key) {
    const items = [];
    for (let page = 1; page <= 30; page++) {
      const result = this.get(
        `${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`,
      );
      const rows = key ? result[key] : result;
      if (!Array.isArray(rows))
        throw Error("Invalid paginated GitHub response");
      items.push(...rows);
      if (rows.length < 100) return items;
    }
    throw Error("GitHub context exceeds pagination limit");
  }
  optional(path) {
    try {
      return this.get(path);
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  }
  role(login) {
    return (
      this.optional(`collaborators/${encodeURIComponent(login)}/permission`)
        ?.role_name ?? "none"
    );
  }
  graphql(query, variables) {
    const response = this.request("graphql", {
      method: "POST",
      data: { query, variables },
    });
    if (response.errors?.length) throw Error("GitHub GraphQL request failed");
    return response.data;
  }
  threads(number) {
    const [owner, name] = this.repository.split("/");
    const nodes = [];
    let cursor = null;
    do {
      const data = this.graphql(
        `query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
        repository(owner:$owner,name:$name) { pullRequest(number:$number) {
          reviewThreads(first:100,after:$cursor) { pageInfo { hasNextPage endCursor }
            nodes { id isResolved comments(first:100) { pageInfo { hasNextPage endCursor }
              nodes { databaseId body updatedAt author { login __typename } } } }
          }
        } }
      }`,
        { owner, name, number, cursor },
      ).repository.pullRequest.reviewThreads;
      for (const thread of data.nodes) {
        const comments = thread.comments.nodes;
        let page = thread.comments.pageInfo;
        while (page.hasNextPage) {
          const more = this.graphql(
            `query($id:ID!,$cursor:String!) { node(id:$id) {
            ... on PullRequestReviewThread { comments(first:100,after:$cursor) {
              pageInfo { hasNextPage endCursor } nodes { databaseId body updatedAt author { login __typename } }
            } }
          } }`,
            { id: thread.id, cursor: page.endCursor },
          ).node.comments;
          comments.push(...more.nodes);
          page = more.pageInfo;
          if (comments.length > 3000) throw Error("Review thread is too large");
        }
        nodes.push({
          id: thread.id,
          isResolved: thread.isResolved,
          comments: comments.map((c) => ({
            id: c.databaseId,
            body: c.body,
            updated_at: c.updatedAt,
            user: c.author
              ? {
                  login: c.author.login,
                  type: c.author.__typename === "Bot" ? "Bot" : "User",
                }
              : null,
          })),
        });
      }
      cursor = data.pageInfo.hasNextPage ? data.pageInfo.endCursor : null;
      if (nodes.length > 3000) throw Error("Too many review threads");
    } while (cursor);
    return nodes;
  }
}

export function eventTarget(api, event, eventName, env) {
  const rerunner = env.GITHUB_TRIGGERING_ACTOR;
  if (
    rerunner &&
    rerunner !== env.GITHUB_ACTOR &&
    !maintainer(api.role(rerunner))
  )
    return null;
  if (eventName === "workflow_dispatch") {
    if (!maintainer(api.role(env.GITHUB_ACTOR))) return null;
    const number = Number(event.inputs.issue_number),
      id = Number(event.inputs.comment_id);
    if (
      !Number.isSafeInteger(number) ||
      number < 1 ||
      !Number.isSafeInteger(id) ||
      id < 1
    )
      throw Error("Invalid dispatch target");
    return { number, commandId: id, automatic: false };
  }
  if (eventName === "issue_comment") {
    if (
      event.action !== "created" ||
      !event.issue?.number ||
      !event.comment?.id
    )
      return null;
    const current = api.optional(`issues/comments/${event.comment.id}`);
    if (
      !current ||
      current.issue_url?.split("/").at(-1) !== String(event.issue.number)
    )
      return null;
    const isCommand =
      current.user?.type === "User" &&
      maintainer(api.role(current.user.login)) &&
      command(current.body);
    if (
      !isCommand &&
      (!event.issue.pull_request ||
        !trustedReview(current.user, {
          [current.user?.login]:
            current.user?.type === "User"
              ? api.role(current.user.login)
              : "none",
        }))
    )
      return null;
    return {
      number: event.issue.number,
      commandId: isCommand ? current.id : null,
      automatic: !isCommand,
    };
  }
  let sha;
  if (eventName === "workflow_run") {
    const run = api.get(`actions/runs/${event.workflow_run?.id}`);
    const allowed = [
      ".github/workflows/slash-agent-review-event.yml",
      ".github/workflows/pr.yml",
      ".github/workflows/haos-e2e-tests.yml",
      ".github/workflows/codeql-quality.yml",
      ".github/workflows/pr-codex-review-delivery.yml",
    ];
    if (
      run.status !== "completed" ||
      !allowed.includes(run.path?.split("@")[0]) ||
      run.head_repository?.full_name !== api.repository
    )
      return null;
    if (
      run.path?.split("@")[0] ===
        ".github/workflows/slash-agent-review-event.yml" &&
      !trustedReview(run.actor, {
        [run.actor?.login]:
          run.actor?.type === "User" ? api.role(run.actor.login) : "none",
      })
    )
      return null;
    sha = run.head_sha;
  } else if (eventName === "status") {
    sha = event.sha;
  } else return null;
  if (!/^[a-f0-9]{40}$/.test(sha ?? "")) return null;
  const prs = api
    .pages(`commits/${sha}/pulls`)
    .filter(
      (pr) =>
        pr.state === "open" &&
        pr.head.sha === sha &&
        pr.head.repo?.full_name === api.repository,
    );
  // Ambiguous branch associations require an explicit command on the desired PR.
  if (prs.length !== 1) return null;
  return { number: prs[0].number, commandId: null, automatic: true };
}

export function collect(api, number, app) {
  let issue = api.get(`issues/${number}`);
  let pr = issue.pull_request ? api.get(`pulls/${number}`) : null;
  let root = number;
  if (pr?.user?.type === "Bot" && pr.user.login === `${app}[bot]`) {
    const origin = new RegExp(`${ORIGIN_MARKER}(\\d+) -->`).exec(pr.body ?? "");
    if (origin) root = Number(origin[1]);
  }
  if (root !== number) issue = api.get(`issues/${root}`);
  const rootComments = api.pages(`issues/${root}/comments`);
  const session = stateFrom(rootComments, app);
  if (root !== number && !session)
    throw Error("PR origin has no owned session checkpoint");
  if (session && session.root !== root)
    throw Error("Session belongs to another issue");
  if (session?.pr) {
    if (pr && session.pr !== pr.number)
      throw Error("Session belongs to another PR");
    pr ??= api.get(`pulls/${session.pr}`);
  }
  const comments = rootComments.concat(
    pr && pr.number !== root ? api.pages(`issues/${pr.number}/comments`) : [],
  );
  const reviews = pr ? api.pages(`pulls/${pr.number}/reviews`) : [];
  const threads = pr ? api.threads(pr.number) : [];
  const roles = {};
  for (const item of [
    ...comments,
    ...reviews,
    ...threads.flatMap((t) => t.comments),
  ]) {
    if (item.user?.type === "User" && !Object.hasOwn(roles, item.user.login))
      roles[item.user.login] = api.role(item.user.login);
  }
  const metadata = api.request(api.root);
  const base = pr?.base.ref ?? metadata.default_branch;
  const branch = pr?.head.ref ?? session?.branch ?? `agents/issue-${root}`;
  if (pr && pr.head.repo?.full_name !== api.repository)
    throw Error(
      "Fork PRs cannot be modified; use an issue to create a repository branch",
    );
  if (
    !branch ||
    branch === metadata.default_branch ||
    branch === base ||
    /[\x00-\x20~^:?*\[\\]/.test(branch) ||
    branch.includes("..")
  )
    throw Error("Unsafe target branch");
  const existingBranch = api.optional(`branches/${encodeURIComponent(branch)}`);
  if (existingBranch?.protected)
    throw Error("Protected branches cannot be modified by the slash agent");
  if (existingBranch && !pr && !session)
    throw Error(
      "The deterministic agent branch already exists without a session checkpoint",
    );
  const head =
    pr?.head.sha ??
    existingBranch?.commit.sha ??
    api.get(`branches/${encodeURIComponent(base)}`).commit.sha;
  if (!/^[a-f0-9]{40}$/.test(head)) throw Error("Invalid source commit");
  const checkRuns = pr
    ? api.pages(`commits/${head}/check-runs?filter=latest`, "check_runs")
    : [];
  const statuses = pr ? api.pages(`commits/${head}/statuses`) : [];
  // Coalesced, secretless wakeups are signals, not tests of the PR's code.
  const checks = checkRuns
    .filter((c) => !(c.app?.id === 15368 && c.name === "Slash review event"))
    .map((c) => ({
      name: c.name,
      appId: c.app?.id,
      complete: c.status === "completed",
      ok: ["success", "neutral", "skipped"].includes(c.conclusion),
      url: c.details_url,
      id: c.id,
    }));
  const contexts = new Set();
  for (const s of statuses) {
    if (contexts.has(s.context)) continue;
    contexts.add(s.context);
    checks.push({
      name: s.context,
      complete: s.state !== "pending",
      ok: s.state === "success",
      url: s.target_url,
      id: s.id,
    });
  }
  const rules = pr ? api.get(`rules/branches/${encodeURIComponent(base)}`) : [];
  const requiredChecks = rules
    .filter((r) => r.type === "required_status_checks")
    .flatMap((r) => r.parameters.required_status_checks);
  const sourceComments = comments
    .filter((c) => c.user?.type === "User" && c.user.login !== "ghhamcp")
    .map((c) => ({
      id: c.id,
      body: c.body,
      updated_at: c.updated_at,
      author: c.user.login,
      maintainer: maintainer(roles[c.user.login]),
    }));
  const feedback = [...comments, ...reviews]
    .filter((c) => trustedReview(c.user, roles))
    .map((c) => ({
      id: c.id,
      body: c.body,
      updated_at: c.updated_at ?? c.submitted_at,
      author: c.user.login,
      state: c.state,
    }));
  const snapshot = {
    app,
    repository: api.repository,
    root,
    issue,
    pr,
    comments,
    roles,
    session,
    sourceComments,
    feedback,
    threads,
    checks,
    requiredChecks,
    base,
    branch,
    head,
  };
  if (Buffer.byteLength(JSON.stringify(snapshot)) > 512000)
    throw Error("Conversation exceeds the 512 KiB context limit");
  return snapshot;
}

export function snapshotGuard(snapshot) {
  return digest({
    root: snapshot.root,
    issue: {
      body: snapshot.issue.body,
      state: snapshot.issue.state,
      locked: snapshot.issue.locked,
    },
    pr: snapshot.pr && {
      number: snapshot.pr.number,
      head: snapshot.pr.head.sha,
      state: snapshot.pr.state,
      base: snapshot.pr.base.ref,
      body: snapshot.pr.body,
      title: snapshot.pr.title,
    },
    comments: snapshot.sourceComments,
    feedback: snapshot.feedback,
    threads: snapshot.threads,
    roles: snapshot.roles,
    session: snapshot.session,
    head: snapshot.head,
  });
}
