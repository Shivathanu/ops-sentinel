"""Code that runs in two places: in the engine, and inside Exasol as Python 3 UDF scripts.

Each module here is self-contained, standard library only with optional numpy, and imports nothing from
ops_sentinel. `store/udfs.py` installs a module's source verbatim as the body of a UDF, so the engine and the
database compute the same fingerprint, similarity or anomaly score, and the logic is unit-tested once, here.
"""
