import { EngineError, type Engine, type ResultReport } from "./client.js";
import type { Config, FixConfig, Source } from "./config.js";
import type { EventSink } from "./log.js";
import { redact, tail } from "./redact.js";
import { describeExit, runCommand } from "./runner.js";
import { createProbe, jobLabel, type Probe } from "./sources.js";

const MAX_ERROR_CHARS = 3800;
const MAX_JOB_CHARS = 200;
const MAX_DETAIL_CHARS = 3500;

export interface MonitorContext {
  config: Config;
  engine: Engine;
  log: EventSink;
  now: () => number;
  sleep: (ms: number, signal?: AbortSignal) => Promise<void>;
  /** Exact secret values (the engine API key) to scrub from anything sent to the engine. */
  secrets: readonly string[];
  fetchImpl?: typeof fetch;
  /** Base delay between attempts of a fix; each retry waits longer. */
  retryDelayMs: number;
}

/** An incident this agent reported and is following until a person decides and the fix, if any, has run. */
export interface Tracking {
  id: number;
  reportedAt: number;
  fixStarted: boolean;
  /** A fix result the engine could not take yet; it is resent on the next tick. */
  pendingResult?: ResultReport;
}

export interface SourceState {
  failing: boolean;
  incident?: Tracking;
  /** How the most recent approved fix ended, so a caller can tell success from failure after tracking stops. */
  lastFixOutcome?: ResultReport["outcome"];
  lastProbeAt: number;
}

export function newState(): SourceState {
  return { failing: false, lastProbeAt: Number.NEGATIVE_INFINITY };
}

/** Probes a source and, on the first failure of an episode, reports it. Later failures of the same episode are not repeated. */
export async function checkSource(ctx: MonitorContext, source: Source, probe: Probe, state: SourceState): Promise<void> {
  const result = await probe();
  if (result.ok) {
    if (state.failing) ctx.log.record("recovered", source.name, { detail: "the source is healthy again" });
    state.failing = false;
    return;
  }
  state.failing = true;
  if (state.incident) return;

  try {
    const incidentId = await ctx.engine.reportFailure({
      source: source.name,
      job: tail(redact(jobLabel(source), ctx.secrets), MAX_JOB_CHARS),
      error: tail(redact(result.error, ctx.secrets), MAX_ERROR_CHARS),
      exit_code: result.exitCode,
      fix_id: source.fix?.id ?? null,
    });
    state.incident = { id: incidentId, reportedAt: ctx.now(), fixStarted: false };
    ctx.log.record("reported", source.name, { incidentId, detail: result.error });
  } catch (error) {
    // The source stays unreported, so the next failing probe tries again.
    ctx.log.record("warning", source.name, { detail: `could not report the failure, will retry: ${describe(error)}` });
  }
}

/** Advances one tracked incident: waits for a decision, runs the approved fix once, reports its result. */
export async function trackIncident(ctx: MonitorContext, source: Source, state: SourceState): Promise<void> {
  const incident = state.incident;
  if (!incident) return;

  if (incident.pendingResult) {
    await sendResult(ctx, source, state, incident, incident.pendingResult);
    return;
  }
  if (incident.fixStarted) return;

  const waitedMs = ctx.now() - incident.reportedAt;
  if (waitedMs > ctx.config.approvalTimeoutMinutes * 60_000) {
    ctx.log.record("timeout", source.name, {
      incidentId: incident.id,
      detail: `no decision within ${ctx.config.approvalTimeoutMinutes} minutes; no longer waiting`,
    });
    state.incident = undefined;
    return;
  }

  let status;
  try {
    status = await ctx.engine.getStatus(incident.id);
  } catch (error) {
    if (error instanceof EngineError && error.status === 404) {
      ctx.log.record("warning", source.name, { incidentId: incident.id, detail: "the engine does not know this incident" });
      state.incident = undefined;
    } else {
      ctx.log.record("warning", source.name, { incidentId: incident.id, detail: `status check failed: ${describe(error)}` });
    }
    return;
  }

  switch (status.status) {
    case "DETECTED":
    case "AWAITING_APPROVAL":
      return;
    case "APPROVED":
      await applyApprovedFix(ctx, source, state, incident, status.fixId);
      return;
    case "REJECTED":
      ctx.log.record("rejected", source.name, { incidentId: incident.id, detail: "a person rejected the fix; nothing was run" });
      state.incident = undefined;
      return;
    default:
      ctx.log.record("closed", source.name, { incidentId: incident.id, detail: `incident is ${status.status}` });
      state.incident = undefined;
  }
}

async function applyApprovedFix(
  ctx: MonitorContext,
  source: Source,
  state: SourceState,
  incident: Tracking,
  approvedFixId: string | null,
): Promise<void> {
  ctx.log.record("approved", source.name, { incidentId: incident.id, detail: `fix '${approvedFixId ?? "none"}' approved` });
  incident.fixStarted = true;

  const fix = source.fix;
  // The engine names a fix by id only. Anything that is not exactly the fix configured here is not run, so a
  // wrong or forged id can never cause a command to execute on this machine.
  if (!fix || fix.id !== approvedFixId) {
    const detail = `approved fix '${approvedFixId ?? "none"}' does not match the fix configured for '${source.name}' (${fix?.id ?? "none"}); nothing was run`;
    ctx.log.record("fix_failed", source.name, { incidentId: incident.id, detail });
    await sendResult(ctx, source, state, incident, { agent: source.name, outcome: "FAILED", detail, attempts: 1 });
    return;
  }

  ctx.log.record("fix_started", source.name, { incidentId: incident.id, detail: `running fix '${fix.id}'` });
  const result = await runFix(ctx, source, fix);
  state.lastFixOutcome = result.outcome;
  ctx.log.record(result.outcome === "SUCCESS" ? "fix_succeeded" : "fix_failed", source.name, {
    incidentId: incident.id,
    detail: result.detail,
  });
  await sendResult(ctx, source, state, incident, result);
}

async function runFix(ctx: MonitorContext, source: Source, fix: FixConfig): Promise<ResultReport> {
  let attempts = 0;
  let detail = "";
  while (attempts < fix.maxAttempts) {
    attempts += 1;
    const run = await runCommand(fix.run, { cwd: source.cwd, timeoutMs: fix.timeoutSeconds * 1000 });
    const ok = run.exitCode === 0 && !run.timedOut;
    detail = redact(tail(`${ok ? "Exited with code 0." : describeExit(run, fix.timeoutSeconds)}\n${run.output}`.trim(), MAX_DETAIL_CHARS), ctx.secrets);
    if (ok) return { agent: source.name, outcome: "SUCCESS", detail, attempts };
    if (attempts < fix.maxAttempts) await ctx.sleep(ctx.retryDelayMs * attempts);
  }
  return { agent: source.name, outcome: "FAILED", detail, attempts };
}

async function sendResult(
  ctx: MonitorContext,
  source: Source,
  state: SourceState,
  incident: Tracking,
  result: ResultReport,
): Promise<void> {
  try {
    await ctx.engine.reportResult(incident.id, result);
  } catch (error) {
    if (error instanceof EngineError && error.status === 409) {
      ctx.log.record("warning", source.name, { incidentId: incident.id, detail: "the engine had already closed this incident" });
    } else {
      incident.pendingResult = result;
      ctx.log.record("warning", source.name, { incidentId: incident.id, detail: `could not report the fix result, will retry: ${describe(error)}` });
      return;
    }
  }
  state.incident = undefined;
}

/** Runs every source until `signal` aborts. One source failing to run its check never stops the others. */
export async function monitorSources(ctx: MonitorContext, signal: AbortSignal): Promise<void> {
  await Promise.all(ctx.config.sources.map((source) => sourceLoop(ctx, source, signal)));
}

async function sourceLoop(ctx: MonitorContext, source: Source, signal: AbortSignal): Promise<void> {
  const probe = createProbe(source, ctx.fetchImpl);
  const state = newState();
  const tickMs = Math.min(ctx.config.pollSeconds, source.intervalSeconds) * 1000;
  ctx.log.record("started", source.name, { detail: `watching (${source.type}, every ${source.intervalSeconds}s)` });

  while (!signal.aborted) {
    try {
      if (ctx.now() - state.lastProbeAt >= source.intervalSeconds * 1000) {
        state.lastProbeAt = ctx.now();
        await checkSource(ctx, source, probe, state);
      }
      await trackIncident(ctx, source, state);
    } catch (error) {
      ctx.log.record("warning", source.name, { detail: `check could not run: ${describe(error)}` });
    }
    await ctx.sleep(tickMs, signal);
  }
}

/** One pass over every source, reporting what is failing. Does not wait for approval. Returns the failing sources. */
export async function checkOnce(ctx: MonitorContext): Promise<string[]> {
  const failing: string[] = [];
  for (const source of ctx.config.sources) {
    const state = newState();
    try {
      await checkSource(ctx, source, createProbe(source, ctx.fetchImpl), state);
    } catch (error) {
      ctx.log.record("warning", source.name, { detail: `check could not run: ${describe(error)}` });
    }
    if (state.failing) failing.push(source.name);
  }
  return failing;
}

export function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
