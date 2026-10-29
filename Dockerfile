# Ops Sentinel engine image. Build from the repository root:  docker build -t ops-sentinel-engine .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# `uv` provides the `uvx` command that starts the Exasol MCP server for the read-only investigation path.
RUN pip install . uv \
    && useradd --create-home --uid 10001 sentinel
USER sentinel

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "ops_sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]
