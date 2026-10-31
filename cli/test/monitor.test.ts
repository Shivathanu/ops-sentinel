import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { EngineError } from "../src/lib/client.js";
import { checkOnce, checkSource, monitorSources, newState, trackIncident } from "../src/lib/monitor.js";
import { createProbe } from "../src/lib/sources.js";
import { API_KEY, CapturedEvents, FakeEngine, makeConfig, makeContext, nodeCommand, tempDir } from "./helpers.js";

interface Rig {
  dir: string;
  engine: FakeEngine;
  events: CapturedEvents;
  clock: { now: number };
  ctx: ReturnType<typeof makeContext>;
  source: ReturnType<typeof makeConfig>["sources"][number];
}

/** A command source that fails until `healed` exists, with a fix that creates it. */
function rig(fixOverrides: Record<string, unknown> = {}, jobError = "ExportError: stale lock"): Rig {
  const dir = tempDir();
  const healed = path.join(dir, "healed");
  const run = nodeCommand(
    dir,
    "job",
    `import { existsSync } from "node:fs"; if (!existsSync(${JSON.stringify(healed)})) { console.error(${JSON.stringify(jobError)}); process.exit(2); }`,
  );
  const fix = nodeCommand(dir, "fix", `import { writeFileSync } from "node:fs"; writeFileSync(${JSON.stringify(healed)}, "x");`);
  const config = makeConfig(
    [{ name: "nightly", type: "command", run, intervalSeconds: 1, timeoutSeconds: 20, fix: { id: "clear-lock", run: fix, ...fixOverrides } }],
    {},
    dir,
  );
  const engine = new FakeEngine();
  const events = new CapturedEvents();
  const clock = { now: 1_000_000 };
  const source = config.sources[0];
  assert.ok(source);
  return { dir, engine, events, clock, ctx: makeContext(config, engine, events, clock), source };
}

test("a failing source is reported once, with its fix id, and not again while the incident is open", async () => {
  const r = rig();
  const state = newState();
  const probe = createProbe(r.source);

  await checkSource(r.ctx, r.source, probe, state);
  await checkSource(r.ctx, r.source, probe, state);

  assert.equal(r.engine.reports.length, 1);
  assert.equal(r.engine.reports[0]?.fix_id, "clear-lock");
  assert.equal(r.engine.reports[0]?.exit_code, 2);
  assert.match(r.engine.reports[0]?.error ?? "", /ExportError: stale lock/);
  assert.equal(state.incident?.id, 100);
});

test("secrets in the error output never leave the machine", async () => {
  const r = rig({}, `login failed password=hunter2hunter2 key=${API_KEY}`);
  await checkSource(r.ctx, r.source, createProbe(r.source), newState());

  const sent = r.engine.reports[0]?.error ?? "";
  assert.ok(!sent.includes("hunter2hunter2"));
  assert.ok(!sent.includes(API_KEY));
});

test("while waiting for approval nothing runs; once approved the fix runs once and its result is reported", async () => {
  const r = rig();
  const state = newState();
  await checkSource(r.ctx, r.source, createProbe(r.source), state);

  await trackIncident(r.ctx, r.source, state);
  assert.equal(r.engine.results.length, 0);
  assert.ok(!existsSync(path.join(r.dir, "healed")));

  r.engine.status = "APPROVED";
  await trackIncident(r.ctx, r.source, state);

  assert.ok(existsSync(path.join(r.dir, "healed")), "the approved fix did not run");
  assert.equal(r.engine.results.length, 1);
  assert.equal(r.engine.results[0]?.result.outcome, "SUCCESS");
  assert.equal(r.engine.results[0]?.result.attempts, 1);
  assert.equal(state.incident, undefined);
  assert.equal(state.lastFixOutcome, "SUCCESS");

  await trackIncident(r.ctx, r.source, state);
  assert.equal(r.engine.results.length, 1, "the fix must not run twice");
});

test("after a successful fix the next healthy probe records the recovery", async () => {
  const r = rig();
  const state = newState();
  const probe = createProbe(r.source);
  await checkSource(r.ctx, r.source, probe, state);
  r.engine.status = "APPROVED";
  await trackIncident(r.ctx, r.source, state);

  await checkSource(r.ctx, r.source, probe, state);

  assert.deepEqual(r.events.types(), ["reported", "approved", "fix_started", "fix_succeeded", "recovered"]);
  assert.equal(state.failing, false);
});

test("a rejected incident stops tracking and runs nothing", async () => {
  const r = rig();
  const state = newState();
  await checkSource(r.ctx, r.source, createProbe(r.source), state);
  r.engine.status = "REJECTED";

  await trackIncident(r.ctx, r.source, state);

  assert.equal(state.incident, undefined);
  assert.ok(!existsSync(path.join(r.dir, "healed")));
  assert.ok(r.events.types().includes("rejected"));
});

test("an approval for a fix id other than the configured one runs nothing and is reported as failed", async () => {
  const r = rig();
  const state = newState();
  await checkSource(r.ctx, r.source, createProbe(r.source), state);
  r.engine.status = "APPROVED";
  r.engine.fixId = "rm-everything";

  await trackIncident(r.ctx, r.source, state);

  assert.ok(!existsSync(path.join(r.dir, "healed")));
  assert.equal(r.engine.results[0]?.result.outcome, "FAILED");
  assert.match(r.engine.results[0]?.result.detail ?? "", /does not match/);
});

test("a failing fix is retried up to maxAttempts and reported as failed", async () => {
  const dir = tempDir();
  const alwaysFails = nodeCommand(dir, "bad-fix", "console.error('still broken'); process.exit(1)");
  const config = makeConfig(
    [{ name: "nightly", type: "command", run: nodeCommand(dir, "job", "process.exit(2)"), intervalSeconds: 1, fix: { id: "fix", run: alwaysFails, maxAttempts: 3 } }],
    {},
    dir,
  );
  const engine = new FakeEngine();
  const ctx = makeContext(config, engine, new CapturedEvents());
  const source = config.sources[0];
  assert.ok(source);
  const state = newState();
  await checkSource(ctx, source, createProbe(source), state);
  engine.status = "APPROVED";

  await trackIncident(ctx, source, state);

  assert.equal(engine.results[0]?.result.outcome, "FAILED");
  assert.equal(engine.results[0]?.result.attempts, 3);
  assert.match(engine.results[0]?.result.detail ?? "", /still broken/);
  assert.equal(state.lastFixOutcome, "FAILED");
});

test("an engine outage while reporting is retried on the next failing probe", async () => {
  const r = rig();
  r.engine.failReports = 1;
  const state = newState();
  const probe = createProbe(r.source);

  await checkSource(r.ctx, r.source, probe, state);
  assert.equal(Boolean(state.incident), false);
  assert.ok(r.events.types().includes("warning"));

  await checkSource(r.ctx, r.source, probe, state);
  assert.equal(state.incident?.id, 100);
});

test("a fix result the engine could not take is resent until it is accepted", async () => {
  const r = rig();
  const state = newState();
  await checkSource(r.ctx, r.source, createProbe(r.source), state);
  r.engine.status = "APPROVED";
  r.engine.failResults = 1;

  await trackIncident(r.ctx, r.source, state);
  assert.equal(r.engine.results.length, 0);
  assert.ok(state.incident?.pendingResult);

  await trackIncident(r.ctx, r.source, state);
  assert.equal(r.engine.results.length, 1);
  assert.equal(state.incident, undefined);
});

test("no decision within the approval window ends tracking without running anything", async () => {
  const r = rig();
  const state = newState();
  await checkSource(r.ctx, r.source, createProbe(r.source), state);

  r.clock.now += 6 * 60_000;
  await trackIncident(r.ctx, r.source, state);

  assert.equal(state.incident, undefined);
  assert.ok(r.events.types().includes("timeout"));
  assert.ok(!existsSync(path.join(r.dir, "healed")));
});

test("an incident the engine does not know is dropped, but a transient status error is retried", async () => {
  const r = rig();
  const state = newState();
  await checkSource(r.ctx, r.source, createProbe(r.source), state);

  r.engine.statusError = new EngineError("cannot reach the engine");
  await trackIncident(r.ctx, r.source, state);
  assert.ok(state.incident, "a transient error must not drop the incident");

  r.engine.statusError = new EngineError("HTTP 404", 404);
  await trackIncident(r.ctx, r.source, state);
  assert.equal(state.incident, undefined);
});

test("checkOnce reports failures without waiting for approval and names the failing sources", async () => {
  const r = rig();

  const failing = await checkOnce(r.ctx);

  assert.deepEqual(failing, ["nightly"]);
  assert.equal(r.engine.reports.length, 1);
});

test("the monitor loop runs a source end to end until it is aborted", async () => {
  const r = rig();
  const controller = new AbortController();
  let ticks = 0;
  r.ctx.sleep = async () => {
    ticks += 1;
    if (ticks === 2) r.engine.status = "APPROVED";
    if (ticks >= 5) controller.abort();
  };
  r.ctx.now = () => r.clock.now + ticks * 2000;

  await monitorSources(r.ctx, controller.signal);

  assert.deepEqual(r.events.types().slice(0, 5), ["started", "reported", "approved", "fix_started", "fix_succeeded"]);
  assert.equal(r.engine.results.length, 1);
});
