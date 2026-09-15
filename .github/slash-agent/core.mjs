import { createHash } from "node:crypto";
import { prose } from "../issue-intake/intake.mjs";

export const MODELS = { astra: "gpt-6-astra", sol: "gpt-5.6-sol" };
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
export const trustedReview = (user, roles) =>
  (user?.type === "User" && maintainer(roles[user.login])) ||
  (user?.type === "Bot" && REVIEW_BOTS.includes(user.login));

export function command(body) {
  const match = /^\/(astra|sol)[ \t]+(\S[\s\S]*)$/.exec((body ?? "").trim());
  if (!match || match[2].length > 12000) return null;
  const text = match[2].trim();
  return {
    model: MODELS[match[1]],
    text,
    action: ["pause", "resume"].includes(text) ? text : "work",
  };
}

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
  const encoded = owned[0].body.split(STATE_MARKER)[1].split(" -->")[0];
  if (!/^[A-Za-z0-9_-]{1,30000}$/.test(encoded))
    throw Error("Invalid slash checkpoint");
  const state = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
  if (
    state.version !== 1 ||
    !Number.isSafeInteger(state.root) ||
    state.root < 1 ||
    !Number.isSafeInteger(state.rounds) ||
    state.rounds < 0 ||
    !Object.values(MODELS).includes(state.model) ||
    !Number.isSafeInteger(state.commandId) ||
    typeof state.summary !== "string" ||
    state.summary.length > 12000 ||
    typeof state.branch !== "string" ||
    typeof state.status !== "string"
  )
    throw Error("Invalid slash checkpoint fields");
  return { ...state, commentId: owned[0].id };
}

export function renderState(state, repository) {
  const { commentId, ...saved } = state;
  const link = state.pr
    ? `\n\nPR: https://github.com/${repository}/pull/${state.pr}`
    : "";
  return (
    `Slash agent: **${state.status}**${link}\n\n${prose(state.summary || "Preparing the requested work.")}\n\n` +
    `Round ${state.rounds}/${MAX_ROUNDS}. Maintainers can use \`/astra pause\`, \`/astra resume\`, or a new \`/sol <request>\`.\n\n` +
    `${STATE_MARKER}${Buffer.from(JSON.stringify(saved)).toString("base64url")} -->`
  );
}

export function feedbackHash(snapshot) {
  return digest({
    body: snapshot.issue.body,
    source: snapshot.sourceComments,
    feedback: snapshot.feedback,
    threads: snapshot.threads
      .filter((t) => !t.isResolved)
      .map((t) => ({ id: t.id, comments: t.comments })),
  });
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

export function decide(snapshot, trigger) {
  if (
    snapshot.issue.state !== "open" ||
    snapshot.issue.locked ||
    snapshot.pr?.state === "closed"
  )
    return { mode: "idle" };
  const commands = snapshot.comments.filter(
    (c) =>
      c.user?.type === "User" &&
      maintainer(snapshot.roles[c.user.login]) &&
      command(c.body),
  );
  commands.sort(
    (a, b) => a.updated_at.localeCompare(b.updated_at) || a.id - b.id,
  );
  const latest = commands.at(-1);
  const previous = snapshot.session;
  if (!latest || (!previous && command(latest.body).action !== "work"))
    return { mode: "idle" };
  const parsed = command(latest.body);
  const changed =
    !previous ||
    previous.commandId !== latest.id ||
    previous.commandUpdatedAt !== latest.updated_at;
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
  const newFailure =
    snapshot.checks.some((c) => c.complete && !c.ok) &&
    previous?.checkedHead !== snapshot.head;
  if (
    !changed &&
    !newFailure &&
    previous.handled === feedback &&
    previous.lastHead === snapshot.head
  ) {
    return {
      mode:
        previous.status !== "ready" && checksReady(snapshot) ? "ready" : "idle",
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
  summary: str(3000),
  tests: str(2000),
  memory: str(6000),
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
    if (!thread || !trustedReview(thread.comments[0]?.user, snapshot.roles))
      throw Error("Response targets an unauthorized review thread");
    seen.add(r.thread_id);
  }
  return result;
}

export function safePath(path) {
  if (
    typeof path !== "string" ||
    path.length > 300 ||
    !/^[A-Za-z0-9_./ -]+$/.test(path) ||
    path.startsWith("/") ||
    path
      .split("/")
      .some(
        (p) => !p || p === "." || p === ".." || p.toLowerCase() === ".git",
      ) ||
    /^(\.github|\.codex|\.claude)(\/|$)/i.test(path) ||
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
