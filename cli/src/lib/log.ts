import { appendFileSync, mkdirSync } from "node:fs";
import path from "node:path";

export type EventType =
  | "started"
  | "reported"
  | "recovered"
  | "approved"
  | "fix_started"
  | "fix_succeeded"
  | "fix_failed"
  | "rejected"
  | "closed"
  | "timeout"
  | "warning";

export interface EventFields {
  incidentId?: number;
  detail?: string;
}

/** Where the monitor records what it did. Kept as an interface so tests can capture events. */
export interface EventSink {
  record(type: EventType, source: string, fields?: EventFields): void;
}

/** Writes each event as a JSON line to a local file and as a readable line to the console. */
export class EventLog implements EventSink {
  private fileFailed = false;

  constructor(
    private readonly file: string | undefined,
    private readonly print: (line: string) => void,
    private readonly clock: () => Date = () => new Date(),
  ) {}

  record(type: EventType, source: string, fields: EventFields = {}): void {
    const ts = this.clock().toISOString();
    const incident = fields.incidentId === undefined ? "" : ` incident #${fields.incidentId}`;
    const detail = fields.detail ? `: ${firstLine(fields.detail)}` : "";
    this.print(`${ts} [${source}] ${type}${incident}${detail}`);
    if (this.file) this.append({ ts, type, source, ...fields });
  }

  private append(entry: Record<string, unknown>): void {
    try {
      mkdirSync(path.dirname(this.file as string), { recursive: true });
      appendFileSync(this.file as string, `${JSON.stringify(entry)}\n`);
    } catch (error) {
      // Say so once instead of failing the watch or repeating the warning on every event.
      if (!this.fileFailed) {
        this.fileFailed = true;
        this.print(`warning: cannot write the event log ${this.file}: ${(error as Error).message}`);
      }
    }
  }
}

function firstLine(text: string): string {
  const line = text.split("\n").find((l) => l.trim() !== "") ?? "";
  return line.length > 200 ? `${line.slice(0, 200)}…` : line;
}
