// JSDOM harness for behavioural tests of in-page <script> bodies.
//
// Reads a JSON request from stdin, evaluates the script inside a JSDOM
// window with stubbed fetch / BroadcastChannel / timers / dialogs, then
// writes a JSON record of observed side effects to stdout. The Python
// wrapper in tests/src/unit/_js_harness.py owns the contract.
//
// Request shape:
//   {
//     script:           string,   // body to eval (no <script> tags)
//     prelude:          string,   // JS to run before `script` (Astro define:vars injection)
//     invoke:           string,   // JS to run after `script` (e.g. "await window.restartAddon()")
//     initialHtml:      string,   // DOM seed (defaults to a blank page)
//     fetchMap:         { [pattern]: { status, ok?, body?, json?, throw?, responses? } },
//     broadcastEvents:  [{ channel, data, delayMs? }],
//     settleMs:         number,   // virtual-time advance after script + invoke (default 120000)
//     language:         "js" | "ts",  // when "ts", `script` and `invoke` are type-stripped via esbuild
//     broadcastChannelUnavailable: bool, // when true, `typeof BroadcastChannel === 'undefined'`
//   }
//
// Response shape:
//   {
//     fetches:    [{ url, method, body }],
//     broadcasts: [{ channel, data }],
//     reloads:    number,
//     alerts:     [string],
//     confirms:   [string],
//     console:    [{ level, args }],
//     status:     string | null,        // last value passed to updateStatus()
//     dom:        string,                // window.document.documentElement.outerHTML
//     errors:     [string],              // thrown errors during script / invoke / timer callbacks
//   }
//
// Only setTimeout / setInterval / Date.now run on the virtual clock.
// new Date() / performance.now() / queueMicrotask continue to report
// wall time — tests against code that reads those sources will see
// real wall-clock values.

import { JSDOM, VirtualConsole } from "jsdom";
import { runInContext } from "node:vm";
import { transformSync } from "esbuild";

function maybeTranspile(source, language) {
  if (!source || language !== "ts") return source;
  // ES2020 target keeps top-level await + optional chaining intact for
  // the current node + jsdom evaluator; `loader: "ts"` skips JSX handling.
  return transformSync(source, {
    loader: "ts",
    target: "es2020",
    format: "esm",
  }).code;
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => {
      buf += chunk;
    });
    process.stdin.on("end", () => resolve(buf));
    process.stdin.on("error", reject);
  });
}

function buildFetchStub(fetchMap, fetches) {
  const counters = new Map();
  return async function fetch(url, init) {
    const method = (init && init.method) || "GET";
    const body = init && init.body != null ? String(init.body) : null;
    fetches.push({ url: String(url), method, body });

    let entry = null;
    let matchedPattern = null;
    for (const [pattern, value] of Object.entries(fetchMap)) {
      if (String(url).includes(pattern)) {
        entry = value;
        matchedPattern = pattern;
        break;
      }
    }
    if (entry == null) {
      // Unmatched URL: 404 by default so tests that forget to register a
      // route get a loud, predictable failure mode.
      entry = { status: 404, body: "" };
    }
    // Sequenced responses: each call advances the index, the last entry
    // sticks after exhaustion (matches "addon comes back online and
    // stays online" — the shape these probe loops need).
    if (Array.isArray(entry.responses)) {
      const idx = counters.get(matchedPattern) ?? 0;
      counters.set(matchedPattern, idx + 1);
      entry = entry.responses[Math.min(idx, entry.responses.length - 1)];
    }
    if (entry.throw) {
      throw new TypeError(entry.throw);
    }
    const status = entry.status ?? 200;
    const ok = entry.ok ?? (status >= 200 && status < 300);
    const bodyText =
      entry.json !== undefined ? JSON.stringify(entry.json) : (entry.body ?? "");
    return {
      ok,
      status,
      async json() {
        // Honour the "truncated body that fails to parse" scenario the
        // saveFeatureFlag fallback specifically guards against.
        return JSON.parse(bodyText);
      },
      async text() {
        return bodyText;
      },
    };
  };
}

class FakeClock {
  constructor(errors) {
    this.now = 0;
    this.nextId = 1;
    this.tasks = new Map(); // id -> { time, fn, interval }
    this._errors = errors;
  }
  setTimeout(fn, delay = 0) {
    const id = this.nextId++;
    this.tasks.set(id, { time: this.now + Math.max(0, delay), fn, interval: null });
    return id;
  }
  setInterval(fn, delay = 0) {
    const id = this.nextId++;
    const period = Math.max(1, delay);
    this.tasks.set(id, { time: this.now + period, fn, interval: period });
    return id;
  }
  clearTimeout(id) {
    this.tasks.delete(id);
  }
  clearInterval(id) {
    this.tasks.delete(id);
  }
  // Advance virtual time up to `untilMs`, firing every ready task in
  // chronological order. Microtasks between tasks are flushed by
  // yielding to setImmediate.
  async advance(untilMs) {
    const target = this.now + untilMs;

    // Drain microtasks first — the script may be awaiting a fetch chain
    // and hasn't registered any timers yet. Checking immediately would
    // return without advancing and leave the script suspended.
    for (let i = 0; i < 50; i++) {
      await new Promise((r) => setImmediate(r));
    }

    const SAFETY_CAP = 100000;
    for (let safety = 0; safety < SAFETY_CAP; safety++) {
      let nextId = null;
      let nextTime = Infinity;
      for (const [id, t] of this.tasks) {
        if (t.time <= target && t.time < nextTime) {
          nextTime = t.time;
          nextId = id;
        }
      }
      if (nextId == null) {
        for (let i = 0; i < 10; i++) {
          await new Promise((r) => setImmediate(r));
        }
        let stillNone = true;
        for (const [, t] of this.tasks) {
          if (t.time <= target) { stillNone = false; break; }
        }
        if (stillNone) break;
        continue;
      }
      const task = this.tasks.get(nextId);
      this.now = task.time;
      if (task.interval != null) {
        task.time = this.now + task.interval;
      } else {
        this.tasks.delete(nextId);
      }
      try {
        task.fn();
      } catch (e) {
        // Side-effect tests don't fail on per-callback throws (the loop
        // must keep draining), but record so a swallowed regression
        // still surfaces in result.errors instead of looking like
        // "feature didn't fire".
        this._errors.push(`timer callback: ${(e && e.stack) || e}`);
      }
      await new Promise((r) => setImmediate(r));
    }
    if (this.tasks.size > 0 && [...this.tasks.values()].some((t) => t.time <= target)) {
      // Hit SAFETY_CAP with timers still ready — likely a runaway
      // self-rescheduling setInterval in the script under test. Record
      // loudly so tests don't see "feature didn't fire" silence.
      this._errors.push(
        `clock.advance: hit ${SAFETY_CAP}-iteration cap with ${this.tasks.size} tasks still pending — likely runaway setInterval`,
      );
    }
    this.now = target;
  }
}

class FakeBroadcastChannel {
  constructor(name, registry, errors) {
    this.name = name;
    this.listeners = [];
    this._registry = registry;
    this._errors = errors;
    if (!registry.byName.has(name)) registry.byName.set(name, []);
    registry.byName.get(name).push(this);
  }
  postMessage(data) {
    this._registry.posts.push({ channel: this.name, data });
    // Spec: deliver to every OTHER same-name channel in the same
    // browsing context; never deliver back to the sender. Honour the
    // contract even though the harness typically opens one channel per
    // test — production code (and future tests) may open multiple.
    const peers = this._registry.byName.get(this.name) || [];
    for (const ch of peers) {
      if (ch !== this) ch._dispatch(data);
    }
  }
  addEventListener(type, fn) {
    if (type === "message") this.listeners.push(fn);
  }
  removeEventListener(type, fn) {
    if (type === "message") {
      this.listeners = this.listeners.filter((f) => f !== fn);
    }
  }
  close() {
    this.listeners = [];
  }
  _dispatch(data) {
    const ev = { data };
    for (const fn of [...this.listeners]) {
      try {
        fn(ev);
      } catch (e) {
        this._errors.push(
          `broadcast listener (${this.name}): ${(e && e.stack) || e}`,
        );
      }
    }
  }
}

async function main() {
  const raw = await readStdin();
  let req;
  try {
    req = JSON.parse(raw);
  } catch (e) {
    process.stderr.write(`harness: invalid JSON request: ${e.message}\n`);
    process.exit(2);
  }

  const fetches = [];
  const broadcasts = [];
  const alerts = [];
  const confirms = [];
  const consoleLog = [];
  const errors = [];
  let reloads = 0;
  let lastStatus = null;

  const fetchMap = req.fetchMap || {};
  const settleMs = req.settleMs ?? 120000;
  const initialHtml =
    req.initialHtml || "<!DOCTYPE html><html><body></body></html>";

  const virtualConsole = new VirtualConsole();
  virtualConsole.on("log", (...args) =>
    consoleLog.push({ level: "log", args: args.map(String) }),
  );
  virtualConsole.on("warn", (...args) =>
    consoleLog.push({ level: "warn", args: args.map(String) }),
  );
  virtualConsole.on("error", (...args) =>
    consoleLog.push({ level: "error", args: args.map(String) }),
  );
  // location.reload() / assign() / href= are unforgeable IDL properties
  // in jsdom — they can't be monkey-patched on the instance. JSDOM's
  // reload impl writes a "Not implemented: navigation" entry to the
  // virtualConsole's jsdomError channel rather than raising in JS.
  // Counting those is the supported way to observe reload calls.
  // Anything else on this channel is a genuine script-level fault —
  // route it to `errors` so tests asserting `not result.errors` catch
  // it instead of burying it in `console` where tests rarely look.
  virtualConsole.on("jsdomError", (err) => {
    const msg = (err && err.message) || String(err);
    if (msg.includes("Not implemented: navigation")) {
      reloads += 1;
    } else {
      errors.push(`jsdom error: ${msg}`);
    }
  });

  const dom = new JSDOM(initialHtml, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
    url: "https://test.local/",
  });
  const { window } = dom;

  const clock = new FakeClock(errors);
  const channelRegistry = { byName: new Map(), posts: broadcasts };

  Object.defineProperty(window, "setTimeout", {
    value: (fn, delay) => clock.setTimeout(fn, delay),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "clearTimeout", {
    value: (id) => clock.clearTimeout(id),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "setInterval", {
    value: (fn, delay) => clock.setInterval(fn, delay),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "clearInterval", {
    value: (id) => clock.clearInterval(id),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window.Date, "now", {
    value: () => clock.now,
    configurable: true,
    writable: true,
  });

  window.fetch = buildFetchStub(fetchMap, fetches);
  if (req.broadcastChannelUnavailable) {
    // Simulate a browsing context where BroadcastChannel is undefined,
    // so the production `typeof BroadcastChannel === 'function'`
    // null-guard branch is exercised.
    delete window.BroadcastChannel;
  } else {
    window.BroadcastChannel = function (name) {
      try {
        return new FakeBroadcastChannel(name, channelRegistry, errors);
      } catch (e) {
        // Surface constructor failures explicitly instead of letting
        // the rendered script see a corrupt object.
        errors.push(`BroadcastChannel ctor: ${(e && e.stack) || e}`);
        throw e;
      }
    };
  }

  window.alert = (msg) => {
    alerts.push(String(msg));
  };
  window.confirm = (msg) => {
    confirms.push(String(msg));
    return true;
  };

  // Status sink so a rendered script's updateStatus() can be observed
  // out-of-band (the DOM dump in the response normally covers it).
  Object.defineProperty(window, "__captureStatus", {
    value: (text) => {
      lastStatus = String(text);
    },
    configurable: true,
    writable: true,
  });

  const prelude = req.prelude || "";
  const language = req.language || "js";
  let scriptBody = req.script || "";
  let invoke = req.invoke || "";
  let transpileFailed = false;
  try {
    scriptBody = maybeTranspile(scriptBody, language);
    invoke = maybeTranspile(invoke, language);
  } catch (e) {
    errors.push(`transpile failure (${language}): ${(e && e.message) || e}`);
    transpileFailed = true;
  }

  if (!transpileFailed) {
    // Run prelude + script at global scope of the JSDOM window's own VM
    // context (via `dom.getInternalVMContext()` + node's `vm.runInContext`)
    // so top-level `function` / `var` declarations land on `window`,
    // matching how a real browser hoists them out of an inline
    // `<script>` block. Wrapping in an async IIFE would scope
    // `function restartAddon() {…}` to the IIFE — invoke's
    // `window.restartAddon()` then resolves to undefined and throws
    // TypeError. Using vm.runInContext rather than eval also keeps the
    // project's "no eval()" lint clean.
    //
    // Invoke runs separately inside an async IIFE so `await` works and
    // we can capture its rejection.
    const vmContext = dom.getInternalVMContext();
    const initProgram = `${prelude}\n${scriptBody}`;
    try {
      runInContext(initProgram, vmContext, { filename: "harness:init" });
    } catch (e) {
      errors.push(`script init: ${(e && e.stack) || e}`);
    }

    const invokeProgram = `
      (async () => { ${invoke} })().then(
        () => { window.__harnessDone = true; },
        (e) => { window.__harnessError = (e && e.stack) || String(e); window.__harnessDone = true; },
      );
    `;
    try {
      runInContext(invokeProgram, vmContext, { filename: "harness:invoke" });
    } catch (e) {
      errors.push(`invoke: ${(e && e.stack) || e}`);
    }
  }

  // Drain microtasks before advancing fake time so the script reaches
  // its first `await fetch(...)` and registers the resulting `.then` chain.
  await new Promise((r) => setImmediate(r));

  // Apply scheduled broadcast injections on the virtual clock.
  for (const evt of req.broadcastEvents || []) {
    const channels = channelRegistry.byName.get(evt.channel) || [];
    const fire = () => {
      for (const ch of channels) ch._dispatch(evt.data);
    };
    if (evt.delayMs && evt.delayMs > 0) {
      clock.setTimeout(fire, evt.delayMs);
    } else {
      fire();
    }
    await new Promise((r) => setImmediate(r));
  }

  await clock.advance(settleMs);

  // One last drain so any post-advance microtasks resolve.
  await new Promise((r) => setImmediate(r));

  if (window.__harnessError) errors.push(window.__harnessError);

  const response = {
    fetches,
    broadcasts,
    reloads,
    alerts,
    confirms,
    console: consoleLog,
    status: lastStatus,
    // outerHTML so body's own attributes (set via
    // `document.body.dataset.foo`) survive into the snapshot, not just
    // body's children.
    dom: window.document.documentElement.outerHTML,
    errors,
  };

  process.stdout.write(JSON.stringify(response));
}

main().catch((e) => {
  process.stderr.write(`harness: fatal: ${(e && e.stack) || e}\n`);
  process.exit(1);
});
