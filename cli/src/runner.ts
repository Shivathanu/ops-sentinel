import { spawn, type ChildProcess } from "node:child_process";

export interface RunOptions {
  cwd: string;
  timeoutMs: number;
  /** Mirror the command's output to this process and give it our stdin: used when wrapping a job. */
  passthrough?: boolean;
  maxOutputChars?: number;
}

export interface RunResult {
  exitCode: number | null;
  signal: NodeJS.Signals | null;
  timedOut: boolean;
  /** The end of the combined stdout and stderr, in arrival order. */
  output: string;
  spawnError?: string;
}

const DEFAULT_MAX_OUTPUT = 64 * 1024;
const KILL_GRACE_MS = 3000;

/** Runs a shell command line to completion and never rejects: every way it can fail is part of the result. */
export function runCommand(command: string, options: RunOptions): Promise<RunResult> {
  const max = options.maxOutputChars ?? DEFAULT_MAX_OUTPUT;
  return new Promise((resolve) => {
    // Its own process group on POSIX, so a timeout can stop the whole tree the shell started.
    const child = spawn(command, {
      cwd: options.cwd,
      shell: true,
      detached: process.platform !== "win32",
      stdio: [options.passthrough ? "inherit" : "ignore", "pipe", "pipe"],
    });
    let output = "";
    let timedOut = false;

    const collect = (chunk: Buffer, sink: NodeJS.WriteStream): void => {
      if (options.passthrough) sink.write(chunk);
      output += chunk.toString("utf8");
      if (output.length > max) output = output.slice(output.length - max);
    };
    child.stdout?.on("data", (chunk: Buffer) => collect(chunk, process.stdout));
    child.stderr?.on("data", (chunk: Buffer) => collect(chunk, process.stderr));

    const timer = setTimeout(() => {
      timedOut = true;
      killTree(child);
    }, options.timeoutMs);

    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ exitCode: null, signal: null, timedOut, output, spawnError: error.message });
    });
    child.on("close", (exitCode, signal) => {
      clearTimeout(timer);
      resolve({ exitCode, signal, timedOut, output });
    });
  });
}

function killTree(child: ChildProcess): void {
  const pid = child.pid;
  if (pid === undefined || process.platform === "win32") {
    child.kill();
    return;
  }
  signalGroup(pid, "SIGTERM");
  setTimeout(() => signalGroup(pid, "SIGKILL"), KILL_GRACE_MS).unref();
}

function signalGroup(pid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-pid, signal);
  } catch (error) {
    // The group is already gone, which is the outcome we wanted.
    if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
  }
}

/** A one-line description of how a finished command ended, for error text. */
export function describeExit(result: RunResult, timeoutSeconds: number): string {
  if (result.spawnError) return `Could not start the command: ${result.spawnError}`;
  if (result.timedOut) return `Timed out after ${timeoutSeconds}s.`;
  if (result.signal) return `Killed by signal ${result.signal}.`;
  return `Exited with code ${result.exitCode}.`;
}
