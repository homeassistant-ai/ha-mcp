import { readFileSync } from "node:fs";

const yaml = readFileSync(
  new URL("../../.github/workflows/close-needs-info.yml", import.meta.url),
  "utf8",
);
const closeWorkflowCode = yaml
  .split("script: |\n")[1]
  .split("\n")
  .map((line) => line.replace(/^ {12}/, ""))
  .join("\n");

export async function runCloseWorkflow(github, hooks = {}) {
  const failures = [];
  await new Function(
    "github",
    "context",
    "core",
    `return (async () => {${closeWorkflowCode}})()`,
  )(
    github,
    { repo: { owner: "test", repo: "repo" } },
    {
      info: hooks.info ?? (() => {}),
      warning: hooks.warning ?? (() => {}),
      setFailed(message) {
        failures.push(message);
      },
    },
  );
  if (failures.length) throw Error(failures.join("\n"));
}

export const bot = "ha-mcp[bot]";
export const user = (login) => ({ login, type: "User" });
export const comment = (id, login, body, extra = {}) => ({
  id,
  user: user(login),
  body,
  html_url: `https://github.com/test/repo/issues/1#issuecomment-${id}`,
  created_at: `2026-09-${String(id + 10).padStart(2, "0")}T00:00:00Z`,
  ...extra,
});
export function snapshot() {
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
export class FakeGitHub {
  constructor(data) {
    this.data = structuredClone(data);
    this.writes = [];
    this.failLabel = false;
    this.delays = [];
    this.delay = async (milliseconds) => this.delays.push(milliseconds);
  }
  request(path, options = {}) {
    if (options.method && options.method !== "GET") {
      this.writes.push({ path, ...options });
      if (path.endsWith("/labels")) {
        if (this.failLabel) throw Error("Label write failed");
        for (const name of options.data.labels) {
          this.data.issue.labels.push({ name });
          this.data.events.push({
            id: 99,
            event: "labeled",
            label: { name },
            actor: { login: bot, type: "Bot" },
            created_at: "2026-09-25T00:00:00Z",
          });
        }
      } else if (path.includes("/labels/")) {
        const name = decodeURIComponent(path.split("/").at(-1));
        this.data.issue.labels = this.data.issue.labels.filter(
          (l) => l.name !== name,
        );
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
    if (path.includes("/issues?")) return [structuredClone(this.data.issue)];
    return structuredClone(this.data.issue);
  }
}
