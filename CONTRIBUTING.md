# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,dashboard]"
ruff check .
pytest
```

The CLI has its own checks (Node 18.17 or newer):

```bash
cd cli && npm install && npm test
```

CI runs the same two commands on Python 3.10 and 3.12.

## Rules of the codebase

- **Reads and writes stay separate.** Investigation goes through `mcp_client.py` only. Anything that writes goes
  through `remediation.py`. Do not add a second path that executes model-suggested SQL.
- **Widen the guardrail with tests.** Add the real remediation pattern to
  `tests/test_guardrail.py` first, then change `guardrail.py`.
- **Known gaps are strict `xfail` tests.** When you fix one, the test starts passing and fails the suite until you
  remove its marker. That is intended.
- **Fail closed.** A missing credential, key or allow-list must refuse the action, never fall back to a weaker path.
- **pyexasol binds parameters by name.** Use `{name}` placeholders with a dict, and `{name!d}` for numeric
  values; the default conversion quotes everything as a string, which Exasol rejects in numeric contexts.
- **Type every signature and log every fallback.** A function that resolves to a default must say so in the log.
- **Data stays synthetic.** Never commit real data, credentials or logs; `.env` is ignored for a reason.

## Pull requests

Keep a change to one concern, include tests, and describe the behavior change rather than the diff.
