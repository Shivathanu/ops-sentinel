"""Builds the CREATE SCRIPT statements that install the platform's Python 3 UDFs into Exasol.

The body of each UDF is the source of a module in `ops_sentinel.udf_lib`, followed by a short `run(ctx)` adapter, so
the code that runs in the database is byte for byte the code the unit tests cover. Installing is idempotent
(CREATE OR REPLACE) and runs after every migration.
"""
from dataclasses import dataclass
from pathlib import Path

from ops_sentinel.udf_lib import anomaly, fingerprint, vector


@dataclass(frozen=True)
class UdfDefinition:
    name: str
    kind: str  # SCALAR or SET
    signature: str
    returns: str  # "RETURNS <type>" or "EMITS (<columns>)"
    library: Path
    adapter: str
    description: str

    def statement(self, schema: str) -> str:
        body = self.library.read_text(encoding="utf-8").rstrip() + "\n\n" + self.adapter.strip() + "\n"
        return (
            f'CREATE OR REPLACE PYTHON3 {self.kind} SCRIPT "{schema}"."{self.name}"({self.signature}) '
            f"{self.returns} AS\n{body}"
        )


UDFS: tuple[UdfDefinition, ...] = (
    UdfDefinition(
        name="FINGERPRINT",
        kind="SCALAR",
        signature="input_text VARCHAR(2000000)",
        returns="RETURNS VARCHAR(32)",
        library=Path(fingerprint.__file__),
        adapter="""
def run(ctx):
    if ctx.input_text is None:
        return None
    return fingerprint(ctx.input_text)
""",
        description="Normalized error fingerprint; correlates historic PIPELINE_LOGS errors in bulk.",
    ),
    UdfDefinition(
        name="VECTOR_COSINE",
        kind="SCALAR",
        signature="query_embedding VARCHAR(2000000), candidate_embedding VARCHAR(2000000)",
        returns="RETURNS DOUBLE",
        library=Path(vector.__file__),
        adapter="""
def run(ctx):
    return cosine(ctx.query_embedding, ctx.candidate_embedding)
""",
        description="Cosine similarity of two base64 float32 embeddings; powers knowledge and memory retrieval.",
    ),
    UdfDefinition(
        name="ROBUST_ZSCORE",
        kind="SET",
        signature="series_key VARCHAR(200), observed_at TIMESTAMP, metric_value DOUBLE",
        returns="EMITS (series_key VARCHAR(200), observed_at TIMESTAMP, metric_value DOUBLE, zscore DOUBLE)",
        library=Path(anomaly.__file__),
        adapter="""
def run(ctx):
    rows = []
    while True:
        rows.append((ctx.series_key, ctx.observed_at, ctx.metric_value))
        if not ctx.next():
            break
    scores = robust_zscores([row[2] for row in rows])
    for (key, observed_at, value), score in zip(rows, scores):
        ctx.emit(key, observed_at, value, score)
""",
        description="Median/MAD anomaly score per series (GROUP BY the series); flags duration and cost outliers.",
    ),
)


def install_statements(schema: str) -> list[str]:
    return [udf.statement(schema) for udf in UDFS]
