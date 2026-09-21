import { prose } from "../issue-intake/intake.mjs";
import {
  checksReady,
  decide,
  digest,
  feedbackHash,
  ORIGIN_MARKER,
  principal,
  trustedComment,
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

function threadSignature(thread) {
  return {
    id: thread.id,
    isResolved: thread.isResolved,
    comments: thread.comments.map((c) => ({
      id: c.id,
      body: c.body,
      updated_at: c.updated_at,
      edited_at: c.edited_at ?? null,
      editor: c.editor ?? null,
      editingVerified: c.editingVerified === true,
      user: c.user && { login: c.user.login, type: c.user.type },
    })),
  };
}

function assertCurrent(api, plan, app, expectedHead, checkThreads = true) {
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
    digest(current.roles) !== digest(plan.snapshot.roles) ||
    (checkThreads &&
      digest(current.threads.map(threadSignature)) !==
        digest(plan.snapshot.threads.map(threadSignature))) ||
    (plan.snapshot.pr && current.pr?.body !== plan.snapshot.pr.body) ||
    current.head !== expectedHead ||
    !command ||
    command.updated_at !== plan.decision.latest.updated_at ||
    principal(command)?.type !== "User" ||
    !trustedComment(command, current.roles)
  )
    throw Error("Source/head/authorization changed during publication");
  const laterControl = current.comments.some(
    (c) =>
      principal(c)?.type === "User" &&
      trustedComment(c, current.roles) &&
      /^\/(astra|sol|terra)\s+/.test(c.body ?? "") &&
      c.updated_at > command.updated_at,
  );
  if (laterControl)
    throw Error("A newer maintainer command superseded this run");
  return current;
}

function ownedComment(comment, app) {
  return (
    comment.user?.type === "Bot" &&
    comment.user.login === `${app}[bot]` &&
    principal(comment)?.type === "Bot" &&
    principal(comment).login === `${app}[bot]`
  );
}

function externalThread(thread, app) {
  return threadSignature({
    ...thread,
    isResolved: false,
    comments: thread.comments.filter((c) => !ownedComment(c, app)),
  });
}

function respond(api, plan, app, state, result, head) {
  for (const response of result.responses) {
    const current = assertCurrent(api, plan, app, head, false);
    validateResult({ ...result, responses: [response] }, current);
    const original = plan.snapshot.threads.find(
      (t) => t.id === response.thread_id,
    );
    const thread = current.threads.find((t) => t.id === response.thread_id);
    if (
      digest(externalThread(thread, app)) !==
      digest(externalThread(original, app))
    )
      throw Error("Review thread changed before response");
    // This key survives a fresh workflow run after partial publication. Only
    // external feedback, command revision or code changes require a new reply.
    const marker = `<!-- slash-response:${digest({
      head,
      command: state.commandId,
      revision: state.commandUpdatedAt,
      source: externalThread(original, app),
    })} -->`;
    const body = `${prose(response.body)}\n\n${marker}`;
    const existing = thread.comments.find(
      (c) => ownedComment(c, app) && c.body.includes(marker),
    );
    let reply = existing;
    if (!existing)
      reply = api.edits([
        api.write(
          `pulls/${state.pr}/comments/${thread.comments[0].id}/replies`,
          { body },
        ),
      ])[0];
    else if (existing.body !== body)
      reply = api.edits([
        api.write(`pulls/comments/${existing.id}`, { body }, "PATCH"),
      ])[0];
    if (!ownedComment(reply, app) || reply.body !== body)
      throw Error("Published reply changed before verification");
    if (response.resolve) {
      const expected = {
        ...thread,
        comments: existing
          ? thread.comments.map((c) => (c.id === existing.id ? reply : c))
          : [...thread.comments, reply],
      };
      const after = assertCurrent(api, plan, app, head, false).threads.find(
        (t) => t.id === response.thread_id,
      );
      if (
        !after ||
        digest(threadSignature(after)) !== digest(threadSignature(expected))
      )
        throw Error("Review thread changed before resolution");
      api.graphql(
        "mutation($id:ID!) { resolveReviewThread(input:{threadId:$id}) { thread { id } } }",
        { id: response.thread_id },
      );
    }
  }
  if (state.pendingSummary) {
    const marker = `<!-- slash-review-summary:${state.pendingSummary} -->`;
    const existing = api
      .edits(api.pages(`issues/${state.pr}/comments`))
      .find((c) => ownedComment(c, app) && c.body.includes(marker));
    const body = `Review update: ${prose(result.summary)}\n\nTests: ${prose(result.tests)}\n\n${result.outcome === "blocked" ? "A maintainer decision is needed; use a new slash command to continue." : "Addressed findings are explained in their threads."}\n\n${marker}`;
    if (!existing)
      api.write(`issues/${state.pr}/comments`, {
        body,
      });
    else if (existing.body !== body)
      api.write(`issues/comments/${existing.id}`, { body }, "PATCH");
  }
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
  if (
    fresh.session &&
    (state.commandId !== fresh.session.commandId ||
      state.commandUpdatedAt !== fresh.session.commandUpdatedAt)
  )
    delete state.pendingSummary;
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
  if (result.responses.length && !state.pendingSummary)
    state.pendingSummary = digest({
      command: state.commandId,
      revision: state.commandUpdatedAt,
      head: fresh.head,
      feedback: d.feedback,
    });
  if (result.outcome === "blocked") {
    if (changes.length || result.responses.some((r) => r.resolve))
      throw Error("Blocked output cannot publish code or resolve findings");
    if (state.pr && state.pendingSummary) {
      state.status = "publishing";
      save(api, state, app);
      respond(api, plan, app, state, result, fresh.head);
      delete state.pendingSummary;
    }
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
    assertCurrent(api, plan, app, head);
    respond(api, plan, app, state, result, head);
    delete state.pendingSummary;
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
  state.status = state.pr
    ? "waiting"
    : result.outcome === "unchanged"
      ? "complete"
      : "blocked";
  if (!state.pr && result.outcome === "unchanged")
    state.summary +=
      "\n\nCompleted on the issue without repository changes; no PR was created.";
  else if (!state.pr)
    state.summary +=
      "\n\nNo code changes were produced, so no PR was created. A maintainer can clarify with a new slash command.";
  save(api, state, app);
  if (state.pr) {
    const current = collect(api, state.root, app);
    // A clarification-only round can finish after the final CI event. Complete
    // readiness here instead of depending on a future unrelated notification.
    if (decide(current, { automatic: true }).mode === "ready") {
      if (current.pr.draft)
        api.graphql(
          "mutation($id:ID!) { markPullRequestReadyForReview(input:{pullRequestId:$id}) { pullRequest { id } } }",
          { id: current.pr.node_id },
        );
      state.status = "ready";
      save(api, state, app);
    }
  }
  return state;
}
