/**
 * Error output leaves the machine twice: to the Ops Sentinel engine, and from there to an LLM provider and a Slack
 * channel. Job logs routinely contain credentials, so everything is scrubbed here before it is sent. This is
 * pattern based and cannot be complete; it is a safety net for the common shapes, not a substitute for keeping
 * secrets out of job output.
 */

const PATTERNS: ReadonlyArray<readonly [RegExp, string]> = [
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, "[REDACTED PRIVATE KEY]"],
  [/(\b[a-z][a-z0-9+.-]*:\/\/)[^\s:@/]+:[^\s@/]+@/gi, "$1[REDACTED]@"],
  [/\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}/gi, "$1 [REDACTED]"],
  [
    /\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|authorization)(["']?\s*[:=]\s*["']?)[^\s"',;&]+/gi,
    "$1$2[REDACTED]",
  ],
  [/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, "[REDACTED AWS KEY]"],
  [/\bxox[abprs]-[A-Za-z0-9-]{10,}/g, "[REDACTED SLACK TOKEN]"],
  [/\bsk-[A-Za-z0-9_-]{16,}/g, "[REDACTED API KEY]"],
  [/\bgh[pousr]_[A-Za-z0-9]{20,}/g, "[REDACTED GITHUB TOKEN]"],
];

/** Scrubs known secret shapes, plus any exact secret values the caller holds (such as the engine API key). */
export function redact(text: string, exactSecrets: readonly string[] = []): string {
  let result = text;
  for (const [pattern, replacement] of PATTERNS) {
    result = result.replace(pattern, replacement);
  }
  for (const secret of exactSecrets) {
    // Very short values would blank out ordinary words, so they are not treated as secrets here.
    if (secret.length >= 6) result = result.split(secret).join("[REDACTED]");
  }
  return result;
}

/** The last `max` characters: the end of a log is where the failure is. */
export function tail(text: string, max: number): string {
  return text.length <= max ? text : text.slice(text.length - max);
}
