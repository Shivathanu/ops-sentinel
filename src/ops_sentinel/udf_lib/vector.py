"""Embedding storage and similarity for retrieval inside Exasol.

Embeddings are stored as base64 of little-endian float32 (a 1536-dimension vector is 8192 characters), which fits a
VARCHAR column and decodes quickly in a UDF. Similarity is a SCALAR UDF, so Exasol parallelizes it across nodes and
the top-k is plain SQL:

    SELECT CHUNK_ID, OPS_SENTINEL.VECTOR_COSINE(:query, EMBEDDING) AS SCORE
    FROM OPS_SENTINEL.KNOWLEDGE_CHUNKS WHERE WORKSPACE_ID = :ws ORDER BY SCORE DESC LIMIT 8
"""
import base64
import functools
import math
import struct

try:  # present in Exasol's Python 3 container; optional in the engine
    import numpy as _np
except ImportError:  # pragma: no cover - depends on the environment
    _np = None


def encode(vector):
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


@functools.lru_cache(maxsize=16)
def _decode_cached(text):
    # The query vector is the same on every row of a search, so caching it avoids decoding it once per chunk.
    return decode(text)


def decode(text):
    raw = base64.b64decode(text)
    if _np is not None:
        return _np.frombuffer(raw, dtype="<f4")
    return struct.unpack(f"<{len(raw) // 4}f", raw)


def cosine(query_text, candidate_text):
    """Cosine similarity in [-1, 1]; None when either side is missing, empty or the dimensions differ."""
    if not query_text or not candidate_text:
        return None
    a = _decode_cached(query_text)
    b = decode(candidate_text)
    if len(a) == 0 or len(a) != len(b):
        return None
    if _np is not None:
        denominator = float(_np.linalg.norm(a) * _np.linalg.norm(b))
        return float(_np.dot(a, b)) / denominator if denominator else None
    dot = sum(x * y for x, y in zip(a, b))  # noqa: B905 - lengths checked above; runs on older UDF Pythons
    denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / denominator if denominator else None
