import { prose } from "../issue-intake/intake.mjs";
import {
  checksReady,
  digest,
  feedbackHash,
  ORIGIN_MARKER,
  renderState,
  validateChanges,
  validateResult,
} from "./core.mjs";
import { collect, snapshotGuard } from "./github.mjs";

function save(api, state, app) {
  const data = { body: renderState(state, api.repository) };
  if (state.commentId)
    api.write(`issues/comments/${state.commentId}`, data, "PATCH");
  else state.commentId = api.write(`issues/${state.root}/comments`, data).id;
}

function assertCurrent(api, plan, app, expectedHead) {
  const current = collect(api, plan.snapshot.root, app);
  const command = current.comments.find(
    (c) => c.id === plan.decision.latest.id,
  );
  if (
    current.issue.state !== "open" ||
    current.issue.locked ||
    current.pr?.state === "closed" ||
    current.issue.body !== plan.snapshot.issue.body ||
    digest(current.sourceComments) !== digest(plan.snapshot.sourceComments) ||
    digest(current.feedback) !== digest(plan.snapshot.feedback) ||
    (plan.snapshot.pr && current.pr?.body !== plan.snapshot.pr.body) ||
    current.head !== expectedHead ||
    !command ||
    command.updated_at !== plan.decision.latest.updated_at ||
    !["maintain", "admin"].includes(current.roles[command.user.login])
  )
    throw Error("Source/head/authorization changed during publication");
  const laterControl = current.comments.some(
    (c) =>
      ["maintain", "admin"].includes(current.roles[c.user?.login]) &&
      /^\/(astra|sol)\s+/.test(c.body ?? "") &&
      c.updated_at > command.updated_at,
  );
  if (laterControl)
    throw Error("A newer maintainer command superseded this run");
  return current;
}

function description(result, root) {
  return `## What does this PR do?\n\n${prose(result.summary)}\n\nRefs #${root}.\n\n## Type of change\n\n- [x] Maintenance / implementation requested by a maintainer\n\n## Testing\n\n${prose(result.tests)}\n\n## Checklist\n\n- [ ] Current CI and review findings verified\n\n${ORIGIN_MARKER}${root} -->`;
}

export function publish(
  api,
  plan,
  artifact,
  app,
  { runId, workerSucceeded = true } = {},
) {
  if (
    plan.repository !== api.repository ||
    plan.app !== app ||
    !/^\d+$/.test(String(runId))
  )
    throw Error("Publication identity mismatch");
  const fresh = collect(api, plan.snapshot.root, app);
  if (snapshotGuard(fresh) !== plan.guard) {
    console.warn(
      "::warning::Slash source changed; stale output was not published. Use a fresh slash command if no new event follows.",
    );
    return { skipped: true };
  }
  const d = plan.decision;
  if (d.mode === "idle") return { skipped: true };
  const state = {
    ...(fresh.session ?? {}),
    version: 1,
    root: fresh.root,
    branch: fresh.branch,
    base: fresh.base,
    pr: fresh.pr?.number ?? null,
    commandId: d.latest.id,
    commandUpdatedAt: d.latest.updated_at,
    model: d.parsed.model,
    task: d.task ?? fresh.session?.task ?? "",
    rounds: d.rounds ?? fresh.session?.rounds ?? 0,
    summary: fresh.session?.summary ?? "",
    status: "working",
  };
  if (d.mode === "pause" || d.mode === "limit") {
    state.status = d.mode === "pause" ? "paused" : "blocked";
    state.summary =
      d.mode === "pause"
        ? "Paused by a maintainer."
        : "Automatic iteration budget reached. Review the remaining work and send a new slash command to continue.";
    save(api, state, app);
    return state;
  }
  if (d.mode === "ready") {
    if (!checksReady(fresh))
      throw Error("Current checks or review threads are not ready");
    if (fresh.pr.draft)
      api.graphql(
        "mutation($id:ID!) { markPullRequestReadyForReview(input:{pullRequestId:$id}) { pullRequest { id } } }",
        { id: fresh.pr.node_id },
      );
    state.status = "ready";
    save(api, state, app);
    return state;
  }
  if (!workerSucceeded) {
    state.status = "blocked";
    state.rounds += 1;
    state.summary = `Worker failed before producing validated output. Inspect Actions run ${runId}, then send a new slash command to retry.`;
    save(api, state, app);
    return state;
  }
  validateResult(artifact.result, fresh);
  validateChanges(artifact.changes);
  if (!/^[a-f0-9-]{36}$/.test(artifact.threadId ?? ""))
    throw Error("Invalid Codex session ID");
  const { result, changes } = artifact;
  if ((result.outcome === "changed") !== changes.length > 0)
    throw Error("Model outcome disagrees with patch contents");
  state.rounds += 1;
  state.summary = `${result.summary}\n\nTests: ${result.tests}\n\nContinuation: ${result.memory}`;
  if (state.summary.length > 12000)
    throw Error("Continuation checkpoint is too large");
  if (result.outcome === "blocked") {
    if (changes.length || result.responses.some((r) => r.resolve))
      throw Error("Blocked output cannot publish code or resolve findings");
    state.status = "blocked";
    save(api, state, app);
    return state;
  }
  // Reserve ownership before creating the deterministic branch. Failed publication
  // remains visible and can be resumed with a new command without adopting a stranger's branch.
  state.status = "publishing";
  save(api, state, app);
  let head = fresh.head;
  if (changes.length) {
    const tree = changes.map((f) => ({
      path: f.path,
      mode: f.mode,
      type: "blob",
      sha:
        f.content === null
          ? null
          : api.write("git/blobs", { encoding: "base64", content: f.content })
              .sha,
    }));
    const parent = api.get(`git/commits/${head}`);
    const nextTree = api.write("git/trees", {
      base_tree: parent.tree.sha,
      tree,
    });
    const commit = api.write("git/commits", {
      message: `${result.title.replace(/[\r\n]/g, " ")}\n\nCodex-Session: ${artifact.threadId}\nSlash-Agent-Run: ${runId}`,
      tree: nextTree.sha,
      parents: [head],
    });
    assertCurrent(api, plan, app, head);
    const ref = api.optional(
      `git/ref/heads/${encodeURIComponent(state.branch)}`,
    );
    if (ref) {
      if (ref.object.sha !== head)
        throw Error("Branch advanced; refusing to overwrite concurrent work");
      api.write(
        `git/refs/heads/${encodeURIComponent(state.branch)}`,
        { sha: commit.sha, force: false },
        "PATCH",
      );
    } else
      api.write("git/refs", {
        ref: `refs/heads/${state.branch}`,
        sha: commit.sha,
      });
    head = commit.sha;
  }
  const ownedBranch =
    fresh.session &&
    api.optional(`git/ref/heads/${encodeURIComponent(state.branch)}`);
  if (!state.pr && (changes.length || ownedBranch)) {
    assertCurrent(api, plan, app, head);
    const matches = api.pages(
      `pulls?state=open&head=${encodeURIComponent(api.repository.split("/")[0] + ":" + state.branch)}`,
    );
    if (matches.length) {
      if (
        matches.length !== 1 ||
        matches[0].user?.login !== `${app}[bot]` ||
        !matches[0].body?.includes(`${ORIGIN_MARKER}${state.root} -->`)
      )
        throw Error("Existing PR ownership mismatch");
      state.pr = matches[0].number;
    } else {
      state.pr = api.write("pulls", {
        head: state.branch,
        base: state.base,
        draft: true,
        title: result.title.replace(/[\r\n]/g, " "),
        body: description(result, state.root),
      }).number;
    }
    // Bind the new PR before resolving review feedback or waiting for its CI.
    state.lastHead = head;
    save(api, state, app);
  }
  if (state.pr) {
    const current = assertCurrent(api, plan, app, head);
    for (const response of result.responses) {
      const original = fresh.threads.find((t) => t.id === response.thread_id);
      const thread = current.threads.find((t) => t.id === response.thread_id);
      if (
        !thread ||
        thread.isResolved ||
        digest(thread.comments) !== digest(original.comments)
      )
        throw Error("Review thread changed before response");
      const marker = `<!-- slash-response:${runId}:${digest(response).slice(0, 16)} -->`;
      if (
        !thread.comments.some(
          (c) => c.user?.login === `${app}[bot]` && c.body?.includes(marker),
        )
      ) {
        api.write(
          `pulls/${state.pr}/comments/${thread.comments[0].id}/replies`,
          { body: `${prose(response.body)}\n\n${marker}` },
        );
      }
      if (response.resolve)
        api.graphql(
          "mutation($id:ID!) { resolveReviewThread(input:{threadId:$id}) { thread { id } } }",
          { id: response.thread_id },
        );
    }
    if (
      fresh.pr?.user?.type === "Bot" &&
      fresh.pr.user.login === `${app}[bot]`
    ) {
      api.write(
        `pulls/${state.pr}`,
        {
          title: result.title.replace(/[\r\n]/g, " "),
          body: description(result, state.root),
        },
        "PATCH",
      );
    }
  }
  state.lastHead = head;
  // Resolve changes are ours; recalculate the feedback digest after those writes.
  // New human input still needs another turn, so retain the admission digest if
  // any non-thread input changed while we were publishing.
  state.handled = feedbackHash({
    ...fresh,
    threads: fresh.threads.map((t) =>
      result.responses.some((r) => r.thread_id === t.id && r.resolve)
        ? { ...t, isResolved: true }
        : t,
    ),
  });
  state.checkedHead = fresh.head;
  state.status = state.pr ? "waiting" : "blocked";
  if (!state.pr)
    state.summary +=
      "\n\nNo code changes were produced, so no PR was created. A maintainer can clarify with a new slash command.";
  save(api, state, app);
  return state;
}
