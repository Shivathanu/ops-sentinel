# Ops Sentinel engine

The Python engine of [Ops Sentinel](../README.md): ingest API, AI SRE agents, Exasol store, channels and the job
worker. The import name is `sentinel`.

```bash
pip install -e "engine[dev]"      # from the repository root
ruff check engine
pytest engine
docker build -t ops-sentinel engine
```

Entry points: `uvicorn sentinel.main:app` (api), `sentinel-worker`, `sentinel-migrate`, `sentinel-validate`.
See [docs/getting-started.md](../docs/getting-started.md) to run it end to end.
