import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import type { Engine, FailureReport, IncidentStatus, ResultReport } from "../src/client.js";
import { parseConfig, type Config } from "../src/config.js";
import type { EventFields, EventSink, EventType } from "../src/log.js";
import type { MonitorContext } from "../src/monitor.js";

export const API_KEY = "test-engine-key-123";
const NODE = JSON.stringify(process.execPath);

export function tempDir(): string {
  return mkdtempSync(path.join(tmpdir(), "sentinel-test-"));
}

/** A shell command line that runs a JavaScript snippet, written to a file so no shell quoting is involved. */
export function nodeCommand(dir: string, name: string, source: string): string {
  const file = path.join(dir, `${name}.mjs`);
  writeFileSync(file, source);
  return `${NODE} ${JSON.stringify(file)}`;
}

export class CapturedEvents implements EventSink {
  readonly events: Array<{ type: EventType; source: string; fields: EventFields }> = [];

  record(type: EventType, source: string, fields: EventFields = {}): void {
    this.events.push({ type, source, fields });
  }

  types(): EventType[] {
    return this.events.map((event) => event.type);
  }
}

/** An in-memory engine whose incident status a test moves by hand. */
export class FakeEngine implements Engine {
  readonly reports: FailureReport[] = [];
  readonly results: Array<{ id: number; result: ResultReport }> = [];
  status = "AWAITING_APPROVAL";
  fixId: string | null = null;
  failReports = 0;
  failResults = 0;
  statusError: Error | undefined;
  private nextId = 100;

  async reportFailure(report: FailureReport): Promise<number> {
    if (this.failReports > 0) {
      this.failReports -= 1;
      throw new Error("engine unreachable");
    }
    this.reports.push(report);
    this.fixId = report.fix_id;
    return this.nextId++;
  }

  async getStatus(incidentId: number): Promise<IncidentStatus> {
    if (this.statusError) throw this.statusError;
    return { incidentId, status: this.status, fixId: this.fixId };
  }

  async reportResult(id: number, result: ResultReport): Promise<void> {
    if (this.failResults > 0) {
      this.failResults -= 1;
      throw new Error("engine unreachable");
    }
    this.results.push({ id, result });
  }
}

export function makeConfig(sources: unknown[], overrides: Record<string, unknown> = {}, baseDir = tempDir()): Config {
  return parseConfig(
    { engine: { url: "http://localhost:8000" }, pollSeconds: 1, approvalTimeoutMinutes: 5, sources, ...overrides },
    { env: { OPS_SENTINEL_API_KEY: API_KEY }, baseDir, requireSources: true },
  );
}

export function makeContext(config: Config, engine: Engine, log: EventSink, clock = { now: 1_000_000 }): MonitorContext {
  return {
    config,
    engine,
    log,
    now: () => clock.now,
    sleep: async () => undefined,
    secrets: [API_KEY],
    retryDelayMs: 1,
  };
}

interface StoredIncident {
  report: FailureReport;
  status: string;
  results: ResultReport[];
}

/** A real HTTP server speaking the engine's three agent endpoints, so the client and CLI are tested over the wire. */
export class FakeEngineServer {
  readonly incidents = new Map<number, StoredIncident>();
  readonly requests: Array<{ method: string; url: string; key: string | undefined }> = [];
  private nextId = 1;
  private server: Server | undefined;
  url = "";

  async start(): Promise<void> {
    this.server = createServer((req, res) => void this.handle(req, res));
    await new Promise<void>((resolve) => this.server?.listen(0, "127.0.0.1", resolve));
    this.url = `http://127.0.0.1:${(this.server?.address() as AddressInfo).port}`;
  }

  async stop(): Promise<void> {
    await new Promise<void>((resolve) => this.server?.close(() => resolve()));
  }

  setStatus(id: number, status: string): void {
    const incident = this.incidents.get(id);
    if (!incident) throw new Error(`no incident ${id}`);
    incident.status = status;
  }

  private async handle(req: IncomingMessage, res: import("node:http").ServerResponse): Promise<void> {
    const key = req.headers["x-api-key"];
    const keyValue = Array.isArray(key) ? key[0] : key;
    this.requests.push({ method: req.method ?? "", url: req.url ?? "", key: keyValue });
    const send = (code: number, body: unknown): void => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };

    if (req.url === "/health") return send(200, { status: "ok" });
    if (keyValue !== API_KEY) return send(401, { detail: "invalid API key" });

    let body = "";
    for await (const chunk of req) body += chunk;
    const parsed: unknown = body ? JSON.parse(body) : undefined;

    if (req.method === "POST" && req.url === "/api/events") {
      const id = this.nextId++;
      this.incidents.set(id, { report: parsed as FailureReport, status: "AWAITING_APPROVAL", results: [] });
      return send(202, { incident_id: id });
    }
    const match = /^\/api\/events\/(\d+)(\/result)?$/.exec(req.url ?? "");
    const incident = match ? this.incidents.get(Number(match[1])) : undefined;
    if (!match || !incident) return send(404, { detail: "no agent incident with that id" });
    if (req.method === "GET") {
      return send(200, { incident_id: Number(match[1]), status: incident.status, fix_id: incident.report.fix_id });
    }
    if (incident.status !== "APPROVED") return send(409, { detail: "incident is not an approved agent incident" });
    const result = parsed as ResultReport;
    incident.results.push(result);
    incident.status = result.outcome === "SUCCESS" ? "REMEDIATED" : "FAILED";
    return send(200, { recorded: true });
  }
}
