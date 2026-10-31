import { FIX_ID_PATTERN, UsageError, type FixConfig, type Source } from "./config.js";
import { newState, trackIncident, describe, type MonitorContext, type SourceState } from "./monitor.js";
import { head, redact, tail } from "./redact.js";
import { runCommand } from "./runner.js";

export interface WrapOptions {
  name: string;
  command: string;
  cwd: string;
  /** Fix to offer for this run: from the matching configured source, or given on the command line. */
  fix?: FixConfig;
  /** Block until a person decides and the fix has run, instead of reporting and exiting. */
  wait: boolean;
}

/**
 * Runs one command like `nohup`-style wrapping: output and exit code pass through untouched. On failure the
 * failure is reported, and with `wait` the process stays until the incident is decided and any approved fix has
 * run. Exit code: the command's own, unless the approved fix succeeded, which exits 0.
 */
export async function runWrapped(ctx: MonitorContext, options: WrapOptions, signal: AbortSignal): Promise<number> {
  const run = await runCommand(options.command, { cwd: options.cwd, timeoutMs: 24 * 60 * 60 * 1000, passthrough: true });
  if (run.exitCode === 0 && !run.timedOut) return 0;
  const exitCode = run.exitCode ?? 1;

  const source: Source = {
    type: "command",
    name: options.name,
    cwd: options.cwd,
    intervalSeconds: 1,
    timeoutSeconds: 1,
    run: options.command,
    ...(options.fix && { fix: options.fix }),
  };
  const state: SourceState = newState();
  try {
    const incidentId = await ctx.engine.reportFailure({
      source: options.name,
      job: head(redact(options.command, ctx.secrets), 200),
      error: tail(redact(run.spawnError ?? run.output, ctx.secrets), 3800),
      exit_code: run.exitCode,
      fix_id: options.fix?.id ?? null,
    });
    state.incident = { id: incidentId, reportedAt: ctx.now(), fixStarted: false };
    ctx.log.record("reported", options.name, { incidentId, detail: `exit code ${exitCode}` });
  } catch (error) {
    // Reporting is best effort here: the job already failed, and its own exit code must not be hidden.
    ctx.log.record("warning", options.name, { detail: `could not report the failure: ${describe(error)}` });
    return exitCode;
  }

  if (!options.wait) return exitCode;

  while (state.incident && !signal.aborted) {
    await trackIncident(ctx, source, state);
    if (state.incident) await ctx.sleep(ctx.config.pollSeconds * 1000, signal);
  }
  return state.lastFixOutcome === "SUCCESS" ? 0 : exitCode;
}

export function parseFixFlags(id: string | undefined, run: string | undefined): FixConfig | undefined {
  if (id === undefined && run === undefined) return undefined;
  if (id === undefined || run === undefined) throw new UsageError("--fix-id and --fix-run must be given together");
  if (!FIX_ID_PATTERN.test(id)) throw new UsageError("--fix-id may only contain letters, digits, '.', '_' and '-' (max 64)");
  return { id, run, maxAttempts: 1, timeoutSeconds: 300 };
}
