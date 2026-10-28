import assert from "node:assert/strict";
import { test } from "node:test";
import { redact, tail } from "../src/redact.js";

test("redacts common credential shapes", () => {
  const cases: Array<[string, string]> = [
    ["Authorization: Bearer abcdef1234567890", "abcdef1234567890"],
    ["connect failed password=hunter2hunter2 host=db", "hunter2hunter2"],
    ['{"api_key": "sk-live-abcdefghijklmnop"}', "sk-live-abcdefghijklmnop"],
    ["postgres://admin:s3cr3tpw@db.internal:5432/app", "s3cr3tpw"],
    ["key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"],
    ["token xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"],
    ["ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"],
  ];
  for (const [input, secret] of cases) {
    const output = redact(input);
    assert.ok(!output.includes(secret), `${secret} survived in: ${output}`);
    assert.ok(output.includes("[REDACTED"), `no marker in: ${output}`);
  }
});

test("redacts a whole private key block", () => {
  const text = "x\n-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef\n-----END RSA PRIVATE KEY-----\ny";
  const output = redact(text);
  assert.ok(!output.includes("MIIEabc"));
  assert.ok(output.startsWith("x\n") && output.endsWith("\ny"));
});

test("redacts exact secret values and ignores very short ones", () => {
  assert.equal(redact("using key supersecretvalue now", ["supersecretvalue"]), "using key [REDACTED] now");
  assert.equal(redact("a b c", ["a"]), "a b c");
});

test("leaves ordinary error text alone", () => {
  const text = "ExportError: target partition is locked (exit 2) at line 118";
  assert.equal(redact(text), text);
});

test("tail keeps the end of a long log", () => {
  assert.equal(tail("0123456789", 4), "6789");
  assert.equal(tail("abc", 10), "abc");
});
