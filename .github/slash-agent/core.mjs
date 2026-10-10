import { createHash } from "node:crypto";
import { prose } from "../issue-intake/intake.mjs";

export const MODELS = {
  astra: "gpt-6.1-sol",
  sol: "gpt-6.1-sol",
  terra: "gpt-5.6-terra",
};
export const REVIEW_BOTS = [
  "coderabbitai[bot]",
  "chatgpt-codex-connector[bot]",
];
export const STATE_MARKER = "<!-- ha-mcp-slash:v1 ";
export const ORIGIN_MARKER = "<!-- ha-mcp-slash-origin:";
export const MAX_ROUNDS = 4;
export const digest = (value) =>
  createHash("sha256").update(JSON.stringify(value)).digest("hex");
export const maintainer = (role) => ["maintain", "admin"].includes(role);
// This maintainer service account is a User, but its own automation output
// must never authorize another paid agent turn.
export const trustedReview = (user, roles) =>
  (user?.type === "User" &&
    user.login !== "ghhamcp" &&
    maintainer(roles[user.login])) ||
  (user?.type === "Bot" && REVIEW_BOTS.includes(user.login));
export function principal(comment) {
  if (comment?.editingVerified !== true) return null;
  return comment.edited_at || comment.editor ? comment.editor : comment.user;
}
export const trustedComment = (comment, roles) =>
  trustedReview(principal(comment), roles);

export function reviewFeedback(comment, roles) {
  if (!trustedComment(comment, roles) || !comment.body?.trim()) return false;
  if (["APPROVED", "DISMISSED", "PENDING"].includes(comment.state)) return false;
  // This connector notice asks for service setup, not a repository change.
  if (principal(comment)?.type === "Bot" &&
      /^To use Codex here, \[create a Codex account and connect to github\]\(https:\/\/chatgpt\.com\/codex\/cloud\/settings\/connectors\)\.?$/i.test(comment.body.trim()))
    return false;
  return true;
}

export function command(body) {
  const match = /^\/(astra|sol|terra)[ \t]+(\S[\s\S]*)$/.exec(
    (body ?? "").trim(),
  );
  if (!match || Buffer.byteLength(match[2], "utf8") > 12000 ||
      Buffer.byteLength(JSON.stringify(match[2]), "utf8") > 12002) return null;
  const text = match[2].trim();
  return {
    model: MODELS[match[1]],
    text,
    action: ["pause", "resume"].includes(text) ? text : "work",
  };
}

export const maintainerCommand = (comment, roles) =>
  comment.user?.type === "User" && comment.user.login !== "ghhamcp" &&
  principal(comment)?.type === "User" && trustedComment(comment, roles)
    ? command(comment.body) : null;
export const commandOrder = (a, b) =>
  a.updated_at.localeCompare(b.updated_at) || a.id - b.id;

export function stateFrom(comments, app) {
  const owned = comments.filter(
    (c) =>
      c.user?.type === "Bot" &&
      c.user.login === `${app}[bot]` &&
      c.body?.includes(STATE_MARKER),
  );
  if (owned.length > 1)
    throw Error(
      "Multiple slash session checkpoints; reconcile them before resuming",
    );
  if (!owned.length) return null;
  if (
    principal(owned[0])?.type !== "Bot" ||
    principal(owned[0]).login !== `${app}[bot]`
  )
    throw Error(
      "Slash checkpoint was modified outside the App or its editor is unknown",
    );
  const encoded = owned[0].body.split(STATE_MARKER)[1].split(" -->")[0];
  if (!/^[A-Za-z0-9_-]{1,50000}$/.test(encoded))
    throw Error("Invalid slash checkpoint");
  let state;
  try {
    state = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
  } catch {
    throw Error("Invalid slash checkpoint JSON");
  }
  if (
    !state ||
    typeof state !== "object" ||
    state.version !== 1 ||
    !Number.isSafeInteger(state.root) ||
    state.root < 1 ||
    !Number.isSafeInteger(state.rounds) ||
    state.rounds < 0 ||
    ![...Object.values(MODELS), "gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol"].includes(state.model) ||
    !Number.isSafeInteger(state.commandId) ||
    typeof state.summary !== "string" ||
    state.summary.length > 12000 ||
    typeof state.task !== "string" ||
    Buffer.byteLength(state.task, "utf8") > 12000 ||
    typeof state.branch !== "string" ||
    typeof state.status !== "string" ||
    (state.handledFeedback !== undefined &&
      (!Array.isArray(state.handledFeedback) ||
        !state.handledFeedback.every((item) => typeof item === "string" && /^[a-f0-9]{64}$/.test(item))))
  )
    throw Error("Invalid slash checkpoint fields");
  return { ...state, commentId: owned[0].id };
}

export function renderState(state, repository) {
  const { commentId, ...saved } = state;
  const encoded = Buffer.from(JSON.stringify(saved)).toString("base64url");
  if (encoded.length > 50000)
    throw Error("Slash checkpoint exceeds the encoded size limit");
  const link = state.pr
    ? `\n\nPR: https://github.com/${repository}/pull/${state.pr}`
    : "";
  const body =
    `Slash agent: **${state.status}**${link}\n\n${prose(state.summary || "Preparing the requested work.")}\n\n` +
    `Round ${state.rounds}/${MAX_ROUNDS}. ${state.status === "closed" ? "This session is closed; start a new request on a separate open issue or PR." : "Maintainers can pause, resume, or send a new `/astra`, `/sol`, or `/terra` request."}\n\n` +
    `${STATE_MARKER}${encoded} -->`;
  if (Buffer.byteLength(body, "utf8") > 65000)
    throw Error("Slash checkpoint exceeds GitHub's comment size limit");
  return body;
}

export function feedbackHash(snapshot) {
  // Reporter material stays in context and the stale-publication guard, but
  // cannot authorize a paid turn through an unrelated CI/status event.
  return digest({
    feedback: snapshot.feedback,
    threads: snapshot.threads
      .filter((t) => !t.isResolved)
      .map((t) => ({
        id: t.id,
        comments: t.comments.filter((c) => reviewFeedback(c, snapshot.roles)),
      }))
      .filter((t) => t.comments.length),
  });
}

export function feedbackItems(snapshot) {
  return [
    ...snapshot.feedback.map((review) => digest({ review })),
    ...snapshot.threads.filter((t) => !t.isResolved).flatMap((thread) =>
      thread.comments.filter((c) => reviewFeedback(c, snapshot.roles)).map((c) =>
        digest({ thread: thread.id, id: c.id, body: c.body,
          updated: c.updated_at, author: principal(c) }))),
  ];
}

export function checksReady(snapshot) {
  // Missing required contexts and missing CI are pending, never implicit success.
  if (
    !snapshot.pr ||
    snapshot.pr.mergeable !== true ||
    !snapshot.checks.length ||
    snapshot.checks.some((c) => !c.complete || !c.ok) ||
    snapshot.threads.some((t) => !t.isResolved)
  )
    return false;
  return snapshot.requiredChecks.every((r) =>
    snapshot.checks.some(
      (c) =>
        c.name === r.context &&
        (!r.integration_id ||
          r.integration_id < 0 ||
          c.appId === r.integration_id) &&
        c.complete &&
        c.ok,
    ),
  );
}

export function failureHash(snapshot) {
  const failed = snapshot.checks.filter((c) => c.complete && !c.ok)
    .map((c) => ({ name: c.name, id: c.id, appId: c.appId }))
    .sort((a, b) => a.name.localeCompare(b.name) || String(a.id).localeCompare(String(b.id)));
  return failed.length ? digest(failed) : null;
}

export function decide(snapshot, trigger) {
  if (snapshot.issue.locked) return { mode: "idle" };
  const commands = snapshot.comments.filter((c) => maintainerCommand(c, snapshot.roles));
  commands.sort(commandOrder);
  const latest = commands.at(-1);
  const previous = snapshot.session;
  if (
    previous &&
    latest &&
    latest.id !== previous.commandId &&
    latest.updated_at <= previous.commandUpdatedAt &&
    trigger.commandId !== latest.id
  )
    return { mode: "idle" };
  if (!latest) return { mode: "idle" };
  const parsed = command(latest.body);
  if (!previous && parsed.action !== "work") return { mode: "idle" };
  const changed =
    !previous ||
    previous.commandId !== latest.id ||
    previous.commandUpdatedAt !== latest.updated_at;
  if (snapshot.issue.state !== "open" || snapshot.pr?.state === "closed") {
    return changed && trigger.commandId === latest.id
      ? { mode: "closed", latest, parsed, rounds: previous?.rounds ?? 0,
          task: parsed.action === "work" ? parsed.text : previous?.task }
      : { mode: "idle" };
  }
  // An ordinary event cannot start a session from an old historical slash command.
  if (!previous && trigger.commandId !== latest.id) return { mode: "idle" };
  if (parsed.action === "pause")
    return {
      mode: previous && previous.status !== "paused" ? "pause" : "idle",
      latest,
      parsed,
    };
  if (previous?.status === "paused" && !changed) return { mode: "idle" };
  if (
    previous &&
    !changed &&
    !trigger.automatic &&
    trigger.commandId !== latest.id
  )
    return { mode: "idle" };
  const feedback = feedbackHash(snapshot);
  const rounds = changed ? 0 : previous.rounds;
  const task = parsed.action === "resume" ? previous?.task : parsed.text;
  if (!task) return { mode: "idle" };
  if (!changed && previous.status === "blocked") return { mode: "idle" };
  const failure = failureHash(snapshot);
  const newFailure = failure !== null &&
    (previous?.checkedHead !== snapshot.head || previous?.checkedFailure !== failure);
  // Removing handled findings is not new work. Older checkpoints retain the
  // aggregate comparison until the next publication records item fingerprints.
  const newFeedback = previous?.handledFeedback
    ? feedbackItems(snapshot).some((item) => !previous.handledFeedback.includes(item))
    : previous?.handled !== feedback;
  if (
    !changed &&
    previous.status !== "publishing" &&
    !newFailure &&
    !newFeedback
  ) {
    return {
      mode:
        checksReady(snapshot) && (previous.status !== "ready" || previous.lastHead !== snapshot.head)
          ? "ready" : "idle",
      latest,
      parsed,
      feedback,
      rounds,
      task,
    };
  }
  if (rounds >= MAX_ROUNDS)
    return { mode: "limit", latest, parsed, feedback, rounds, task };
  return { mode: "code", latest, parsed, feedback, rounds, task };
}

const str = (maxLength) => ({ type: "string", minLength: 1, maxLength });
const obj = (properties) => ({
  type: "object",
  additionalProperties: false,
  properties,
  required: Object.keys(properties),
});
export const resultSchema = obj({
  title: str(120),
  // Per-field limits bound continuation memory. renderState separately checks
  // JSON/base64 expansion and the public comment's UTF-8 size.
  summary: str(2000),
  tests: str(1000),
  memory: str(2000),
  outcome: { type: "string", enum: ["changed", "unchanged", "blocked"] },
  responses: {
    type: "array",
    maxItems: 30,
    items: obj({
      thread_id: str(120),
      body: str(3000),
      resolve: { type: "boolean" },
    }),
  },
});

export function validateResult(result, snapshot) {
  const keys = Object.keys(resultSchema.properties);
  if (
    !result ||
    typeof result !== "object" ||
    Object.keys(result).sort().join() !== keys.sort().join()
  )
    throw Error("Invalid model result fields");
  for (const name of ["title", "summary", "tests", "memory"]) {
    if (
      typeof result[name] !== "string" ||
      !result[name].trim() ||
      result[name].length > resultSchema.properties[name].maxLength
    )
      throw Error(`Invalid result.${name}`);
  }
  if (
    !resultSchema.properties.outcome.enum.includes(result.outcome) ||
    !Array.isArray(result.responses) ||
    result.responses.length > 30
  )
    throw Error("Invalid result outcome/responses");
  const seen = new Set();
  for (const r of result.responses) {
    if (
      Object.keys(r).sort().join() !== "body,resolve,thread_id" ||
      typeof r.body !== "string" ||
      !r.body.trim() ||
      r.body.length > 3000 ||
      typeof r.resolve !== "boolean" ||
      seen.has(r.thread_id)
    )
      throw Error("Invalid review response");
    const thread = snapshot.threads.find(
      (t) => t.id === r.thread_id && !t.isResolved,
    );
    if (
      !thread ||
      !thread.comments.some((c) => reviewFeedback(c, snapshot.roles))
    )
      throw Error("Response targets an unauthorized review thread");
    seen.add(r.thread_id);
  }
  return result;
}

export function safePath(path) {
  if (
    typeof path !== "string" ||
    path.length > 300 ||
    /[\\:\x00-\x1f\x7f]/.test(path) ||
    path.startsWith("/") ||
    path
      .split("/")
      .some(
        (p) => !p || p === "." || p === ".." || p.toLowerCase() === ".git",
      ) ||
    /^(\.github|\.codex|\.claude)(\/|$)/i.test(path) ||
    /(^|\/)(AGENTS|CLAUDE)\.md$/i.test(path) ||
    /(^|\/)(\.env(?:\..*)?|auth\.json)$/i.test(path)
  )
    throw Error("Patch contains a prohibited path");
  return path;
}

export function validateChanges(changes) {
  if (!Array.isArray(changes) || changes.length > 80)
    throw Error("Patch exceeds the 80-file limit");
  let bytes = 0;
  const seen = new Set();
  for (const file of changes) {
    safePath(file.path);
    if (
      seen.has(file.path) ||
      !["100644", "100755"].includes(file.mode) ||
      (file.content !== null &&
        (typeof file.content !== "string" ||
          !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
            file.content,
          )))
    )
      throw Error("Invalid patch entry");
    seen.add(file.path);
    bytes +=
      file.content === null ? 0 : Buffer.byteLength(file.content, "base64");
  }
  if (bytes > 2 * 1024 * 1024) throw Error("Patch exceeds the 2 MiB limit");
  return changes;
}
