"""Stable fingerprints for error text, so repeats of one failure correlate into one incident.

Volatile tokens (timestamps, UUIDs, hex ids, IP addresses, numbers) are replaced by placeholders before hashing, so
"timeout after 30012 ms on 10.0.0.7" and "timeout after 29870 ms on 10.0.0.9" share a fingerprint.
"""
import hashlib
import re

_RULES = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize(text):
    value = (text or "").lower()
    for pattern, placeholder in _RULES:
        value = pattern.sub(placeholder, value)
    return value.strip()


def fingerprint(*parts):
    """32 hex characters. Parts are normalized individually and joined with a separator that cannot occur in them."""
    joined = "\x1f".join(normalize(str(part)) for part in parts if part is not None)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]
