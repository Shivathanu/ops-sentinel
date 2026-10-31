import math

import pytest

from sentinel.udf_lib import anomaly, fingerprint, vector


def test_volatile_tokens_do_not_change_the_fingerprint() -> None:
    a = fingerprint.fingerprint("etl", "2026-10-31T02:00:01Z timeout after 30012 ms on 10.0.0.7 run 4f1c2a9be0d4ff12")
    b = fingerprint.fingerprint("etl", "2026-11-01 02:00:59.123 timeout after 29870 ms on 10.0.0.9 run 0a9b8c7d6e5f4a3b")

    assert a == b
    assert len(a) == 32


def test_different_failures_and_different_sources_get_different_fingerprints() -> None:
    assert fingerprint.fingerprint("etl", "connection refused") != fingerprint.fingerprint("etl", "disk full")
    assert fingerprint.fingerprint("etl", "disk full") != fingerprint.fingerprint("api", "disk full")


def test_parts_cannot_run_into_each_other() -> None:
    assert fingerprint.fingerprint("ab", "c") != fingerprint.fingerprint("a", "bc")


def test_vectors_round_trip_and_similarity_is_cosine() -> None:
    a, b = [1.0, 0.0, 2.0], [2.0, 0.0, 4.0]
    encoded = vector.encode(a)

    assert list(vector.decode(encoded)) == pytest.approx(a)
    assert vector.cosine(encoded, vector.encode(b)) == pytest.approx(1.0)
    assert vector.cosine(encoded, vector.encode([0.0, 1.0, 0.0])) == pytest.approx(0.0)
    assert vector.cosine(encoded, vector.encode([-1.0, 0.0, -2.0])) == pytest.approx(-1.0)


@pytest.mark.parametrize("other", [None, "", vector.encode([1.0, 2.0]), vector.encode([0.0, 0.0, 0.0])])
def test_similarity_is_null_when_it_is_undefined(other: str | None) -> None:
    assert vector.cosine(vector.encode([1.0, 0.0, 2.0]), other) is None


def test_a_spike_scores_high_and_normal_points_score_low() -> None:
    durations = [60, 62, 59, 61, 60, 63, 58, 61, 600]
    scores = anomaly.robust_zscores(durations)

    assert scores[-1] > 10
    assert all(abs(s) < 3 for s in scores[:-1])


def test_flat_series_with_one_outlier_still_flags_it() -> None:
    scores = anomaly.robust_zscores([5, 5, 5, 5, 5, 50])

    assert scores[-1] > 3
    assert scores[0] == pytest.approx(0.0)


def test_short_constant_and_null_series_are_safe() -> None:
    assert anomaly.robust_zscores([1, 2]) == [0.0, 0.0]
    assert anomaly.robust_zscores([7, 7, 7, 7]) == [0.0, 0.0, 0.0, 0.0]
    scores = anomaly.robust_zscores([1, None, 2, 3, 100])
    assert scores[1] is None
    assert not any(isinstance(s, float) and math.isnan(s) for s in scores)
