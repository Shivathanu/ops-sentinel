export class EngineError extends Error {
  constructor(
    message: string,
    /** The HTTP status, or undefined when the engine could not be reached at all. */
    readonly status?: number,
  ) {
    super(message);
    this.name = "EngineError";
  }
}

export interface FailureReport {
  source: string;
  job: string;
  error: string;
  exit_code: number | null;
  fix_id: string | null;
}

export interface IncidentStatus {
  incidentId: number;
  status: string;
  fixId: string | null;
}

export interface ResultReport {
  agent: string;
  outcome: "SUCCESS" | "FAILED";
  detail: string;
  attempts: number;
}

/** The three calls the agent makes to the Ops Sentinel engine. */
export interface Engine {
  reportFailure(report: FailureReport): Promise<number>;
  getStatus(incidentId: number): Promise<IncidentStatus>;
  reportResult(incidentId: number, result: ResultReport): Promise<void>;
}

export class EngineClient implements Engine {
  constructor(
    private readonly baseUrl: string,
    private readonly apiKey: string,
    private readonly timeoutMs = 10_000,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  async reportFailure(report: FailureReport): Promise<number> {
    const body = await this.request("POST", "/api/events", report);
    return readNumber(body, "incident_id");
  }

  async getStatus(incidentId: number): Promise<IncidentStatus> {
    const body = await this.request("GET", `/api/events/${incidentId}`);
    const fixId = isRecord(body) ? body["fix_id"] : undefined;
    return {
      incidentId: readNumber(body, "incident_id"),
      status: readString(body, "status"),
      fixId: typeof fixId === "string" ? fixId : null,
    };
  }

  async reportResult(incidentId: number, result: ResultReport): Promise<void> {
    await this.request("POST", `/api/events/${incidentId}/result`, result);
  }

  /** Reachability check for `sentinel check`; /health needs no key. */
  async health(): Promise<void> {
    const response = await this.send("GET", "/health", undefined, false);
    if (!response.ok) throw new EngineError(`GET /health -> HTTP ${response.status}`, response.status);
  }

  private async request(method: string, path: string, payload?: unknown): Promise<unknown> {
    const response = await this.send(method, path, payload, true);
    const text = await response.text();
    if (!response.ok) {
      throw new EngineError(`${method} ${path} -> HTTP ${response.status}: ${text.slice(0, 300)}`, response.status);
    }
    try {
      return JSON.parse(text);
    } catch {
      throw new EngineError(`${method} ${path} returned a body that is not JSON`, response.status);
    }
  }

  private async send(method: string, path: string, payload: unknown, authenticated: boolean): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (authenticated) headers["X-API-Key"] = this.apiKey;
    if (payload !== undefined) headers["Content-Type"] = "application/json";
    try {
      return await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: payload === undefined ? undefined : JSON.stringify(payload),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      throw new EngineError(`cannot reach the engine at ${this.baseUrl}: ${reason}`);
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readNumber(body: unknown, key: string): number {
  const value = isRecord(body) ? body[key] : undefined;
  if (typeof value !== "number") throw new EngineError(`engine response is missing a numeric '${key}'`);
  return value;
}

function readString(body: unknown, key: string): string {
  const value = isRecord(body) ? body[key] : undefined;
  if (typeof value !== "string") throw new EngineError(`engine response is missing a string '${key}'`);
  return value;
}
