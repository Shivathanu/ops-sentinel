"""Robust anomaly scores for metric series (pipeline duration, rows processed, query cost).

Median and MAD rather than mean and standard deviation, so one huge spike does not hide itself by inflating the
spread. Runs as a SET UDF, one call per series:

    SELECT OPS_SENTINEL.ROBUST_ZSCORE(PIPELINE_ID, EXECUTION_TS, DURATION_SECONDS)
    FROM SENTINEL.PIPELINE_LOGS GROUP BY PIPELINE_ID
"""
import statistics

# Scales the MAD to match the standard deviation for normally distributed data.
_MAD_SCALE = 0.6745


def robust_zscores(values):
    """One score per value, in input order. Scores are 0.0 when a series is too short or has no spread."""
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 3:
        return [0.0 if v is not None else None for v in values]
    median = statistics.median(clean)
    mad = statistics.median(abs(v - median) for v in clean)
    if mad > 0:
        sigma = mad / _MAD_SCALE
    else:
        # More than half the points are identical, so the MAD is 0. The mean absolute deviation still moves;
        # it is 0.7979 sigma for normally distributed data.
        sigma = sum(abs(v - median) for v in clean) / len(clean) / 0.7979
    if sigma == 0:
        return [0.0 if v is not None else None for v in values]
    return [None if v is None else (float(v) - median) / sigma for v in values]
