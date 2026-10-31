import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { WebhookSource } from "./config.js";
import { describe, newState, reportFailure, trackIncident, type MonitorContext, type SourceState } from "./monitor.js";

const MAX_BODY_BYTES = 64 * 1024;
const ERROR_FIELDS = ["error", "message", "text", "title", "summary", "description"] as const;
const KEY_FIELDS = ["key", "id", "fingerprint", "alert_id"] as const;

export interface WebhookEvent {
  /** Identifies the thing that is failing, so repeats while an incident is open are not reported again. */
  key: string;
  error: string;
}

/** Turns a posted body into an event. JSON with an error-like field is used as is; anything else is kept as text. */
export function parseWebhookBody(body: string): WebhookEvent {
  let error = body.trim();
  let key: string | undefined;
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>;
      const parts = ERROR_FIELDS.map((field) => record[field]).filter((v): v is string => typeof v === "string" && v !== "");
      error = parts.length > 0 ? parts.join("\n") : JSON.stringify(parsed);
      const rawKey = KEY_FIELDS.map((field) => record[field]).find((v) => typeof v === "string" || typeof v === "number");
      key = rawKey === undefined ? undefined : String(rawKey);
    }
  } catch {
    // Not JSON: the raw text is the error.
  }
  if (error === "") error = "(empty webhook body)";
  return { key: key ?? createHash("sha256").update(error).digest("hex").slice(0, 16), error };
}

function secretMatches(presented: string | undefined, expected: string): boolean {
  if (presented === undefined) return false;
  const a = Buffer.from(presented);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

function presentedSecret(req: IncomingMessage): string | undefined {
  const direct = req.headers["x-sentinel-secret"];
  if (typeof direct === "string") return direct;
  const auth = req.headers["authorization"];
  return typeof auth === "string" && /^bearer /i.test(auth) ? auth.slice(7).trim() : undefined;
}

async function readBody(req: IncomingMessage): Promise<string | undefined> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) return undefined;
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function reply(res: ServerResponse, code: number, body: unknown): void {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

/**
 * Listens for failure alerts pushed by other tools (CI, status pages, monitors) and treats each new key as a failure
 * of this source. Runs until `signal` aborts. The sender gets 202 only after the engine accepted the incident, so a
 * sender that retries on errors never loses an alert when the engine is down.
 */
export async function runWebhookSource(ctx: MonitorContext, source: WebhookSource, signal: AbortSignal): Promise<void> {
  const open = new Map<string, SourceState>();

  const handle = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const url = (req.url ?? "").split("?")[0];
    if (url !== source.path) return reply(res, 404, { detail: "not found" });
    if (req.method !== "POST") return reply(res, 405, { detail: "use POST" });
    if (!secretMatches(presentedSecret(req), source.secret)) {
      ctx.log.record("warning", source.name, { detail: "rejected a webhook with a missing or wrong secret" });
      return reply(res, 401, { detail: "invalid or missing secret" });
    }
    const body = await readBody(req);
    if (body === undefined) return reply(res, 413, { detail: "body too large" });

    const event = parseWebhookBody(body);
    if (open.has(event.key)) return reply(res, 202, { status: "duplicate", key: event.key });
    // Claim the key before awaiting, so two simultaneous deliveries of one alert cannot both report it.
    const state = newState();
    open.set(event.key, state);
    const accepted = await reportFailure(ctx, source, state, event.error, null);
    if (!accepted) {
      open.delete(event.key);
      return reply(res, 502, { detail: "the Ops Sentinel engine could not take the incident; retry" });
    }
    reply(res, 202, { status: "accepted", key: event.key, incident_id: state.incident?.id });
  };

  const server: Server = createServer((req, res) => {
    handle(req, res).catch((error: unknown) => {
      ctx.log.record("warning", source.name, { detail: `webhook handler failed: ${describe(error)}` });
      if (!res.headersSent) reply(res, 500, { detail: "internal error" });
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(source.port, source.host, resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : source.port;
  ctx.log.record("started", source.name, { detail: `listening on http://${source.host}:${port}${source.path}` });

  try {
    while (!signal.aborted) {
      for (const [key, state] of open) {
        try {
          await trackIncident(ctx, source, state);
        } catch (error) {
          ctx.log.record("warning", source.name, { detail: `check could not run: ${describe(error)}` });
        }
        if (!state.incident) open.delete(key);
      }
      await ctx.sleep(ctx.config.pollSeconds * 1000, signal);
    }
  } finally {
    await new Promise<void>((resolve) => {
      server.close(() => resolve());
      server.closeAllConnections();
    });
  }
}
