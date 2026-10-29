import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { Flags } from "@oclif/core";
import { DEFAULT_LOG, SentinelCommand } from "../base.js";
import { UsageError } from "../lib/config.js";

export default class Status extends SentinelCommand {
  static override summary = "Show recent events from the local log";
  static override flags = {
    log: Flags.string({ description: "event log (default: .sentinel/events.jsonl in --dir)" }),
    n: Flags.string({ char: "n", description: "how many events to show", default: "20" }),
    json: Flags.boolean({ description: "print raw JSON lines" }),
  };

  async run(): Promise<void> {
    const { flags } = await this.parse(Status);
    const file = path.resolve(this.projectDir(flags.dir), flags.log ?? DEFAULT_LOG);
    const count = Number(flags.n);
    if (!Number.isInteger(count) || count < 1) throw new UsageError("-n must be a positive whole number");
    if (!existsSync(file)) {
      this.log(`No events recorded yet (${file} does not exist).`);
      return;
    }
    const lines = readFileSync(file, "utf8").split("\n").filter((line) => line.trim() !== "").slice(-count);
    for (const line of lines) {
      if (flags.json) {
        this.log(line);
        continue;
      }
      try {
        const e = JSON.parse(line) as { ts: string; source: string; type: string; incidentId?: number; detail?: string };
        const incident = e.incidentId === undefined ? "" : ` #${e.incidentId}`;
        this.log(`${e.ts}  ${e.source}  ${e.type}${incident}${e.detail ? `  ${e.detail.split("\n")[0]}` : ""}`);
      } catch {
        this.log(`(unreadable log line) ${line.slice(0, 120)}`);
      }
    }
  }
}
