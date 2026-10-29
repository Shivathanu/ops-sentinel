import { open, stat } from "node:fs/promises";
import { StringDecoder } from "node:string_decoder";
import type { CommandSource, FileSource, HttpSource, Source } from "./config.js";
import { describeExit, runCommand } from "./runner.js";

export interface ProbeResult {
  ok: boolean;
  /** What went wrong, empty when `ok`. */
  error: string;
  exitCode: number | null;
}

/** Looks at one automation source once. Throws only when the check itself could not run. */
export type Probe = () => Promise<ProbeResult>;

const MAX_MATCHED_LINES = 20;
const MAX_READ_BYTES = 1024 * 1024;

export function createProbe(source: Source, fetchImpl: typeof fetch = fetch): Probe {
  switch (source.type) {
    case "command":
      return commandProbe(source);
    case "file":
      return fileProbe(source);
    case "http":
      return httpProbe(source, fetchImpl);
    case "webhook":
      // A webhook is pushed to us, so there is nothing to poll; webhook.ts handles it.
      throw new Error(`source '${source.name}' is a webhook and has no probe`);
  }
}

function commandProbe(source: CommandSource): Probe {
  return async () => {
    const result = await runCommand(source.run, { cwd: source.cwd, timeoutMs: source.timeoutSeconds * 1000 });
    if (result.exitCode === 0 && !result.timedOut) return { ok: true, error: "", exitCode: 0 };
    const error = `${describeExit(result, source.timeoutSeconds)}\n${result.output}`.trim();
    return { ok: false, error, exitCode: result.exitCode };
  };
}

function httpProbe(source: HttpSource, fetchImpl: typeof fetch): Probe {
  return async () => {
    try {
      const response = await fetchImpl(source.url, { signal: AbortSignal.timeout(source.timeoutSeconds * 1000) });
      // The body is not needed; cancelling it frees the connection.
      await response.body?.cancel();
      if (source.expectStatus.includes(response.status)) return { ok: true, error: "", exitCode: null };
      return { ok: false, error: `GET ${source.url} returned HTTP ${response.status}; expected ${source.expectStatus.join(" or ")}.`, exitCode: null };
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      return { ok: false, error: `GET ${source.url} failed: ${reason}`, exitCode: null };
    }
  };
}

/**
 * Follows a log file like `tail -f`. Lines already in the file when the watcher starts are history and are
 * skipped; a file that appears later, or shrinks (rotation, truncation), is read from the start.
 */
function fileProbe(source: FileSource): Probe {
  let offset: number | undefined;
  let carry = "";
  let firstLook = true;
  const decoder = new StringDecoder("utf8");

  return async () => {
    let size: number;
    try {
      size = (await stat(source.path)).size;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      offset = undefined;
      firstLook = false;
      return { ok: true, error: "", exitCode: null };
    }

    if (offset === undefined) offset = firstLook ? size : 0;
    firstLook = false;
    if (size < offset) {
      offset = 0;
      carry = "";
    }
    if (size === offset) return { ok: true, error: "", exitCode: null };

    const length = Math.min(size - offset, MAX_READ_BYTES);
    const handle = await open(source.path, "r");
    try {
      const buffer = Buffer.alloc(length);
      const { bytesRead } = await handle.read(buffer, 0, length, offset);
      offset += bytesRead;
      const lines = (carry + decoder.write(buffer.subarray(0, bytesRead))).split("\n");
      carry = lines.pop() ?? "";
      const matched = lines.filter((line) => source.pattern.test(line));
      if (matched.length === 0) return { ok: true, error: "", exitCode: null };
      return { ok: false, error: matched.slice(-MAX_MATCHED_LINES).join("\n"), exitCode: null };
    } finally {
      await handle.close();
    }
  };
}

/** What identifies the failing thing in the incident, with no secrets in it. */
export function jobLabel(source: Source): string {
  switch (source.type) {
    case "command":
      return source.run;
    case "file":
      return `log file ${source.path}`;
    case "http":
      return `GET ${source.url}`;
    case "webhook":
      return `webhook ${source.path}`;
  }
}
