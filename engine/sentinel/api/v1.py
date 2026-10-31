"""Platform API.

    POST /v1/ingest/{workspace}/{source}                 any alerting tool posts here (token per source)
    GET  /v1/workspaces                                  configured workspaces and their sources
    GET  /v1/workspaces/{workspace}/incidents            recent incidents
    GET  /v1/workspaces/{workspace}/incidents/{id}       one incident with its timeline
    GET  /v1/healthz                                     the process is up
    GET  /v1/readyz                                      config is valid and Exasol answers
"""
import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from sentinel.config import settings
from sentinel.core.models import IncidentStatus
from sentinel.ingest.service import IngestFailure, ingest
from sentinel.platform import Platform, get_platform
from sentinel.security import require_api_key

logger = logging.getLogger("sentinel.api.v1")

router = APIRouter(prefix="/v1", tags=["v1"])


def platform_dependency() -> Platform:
    platform = get_platform()
    if platform is None:
        raise HTTPException(status_code=503, detail="no workspace spec configured (set OPS_SENTINEL_SPEC_FILE)")
    return platform


PlatformDep = Annotated[Platform, Depends(platform_dependency)]


def _workspace_or_404(platform: Platform, workspace: str) -> None:
    if platform.workspace(workspace) is None:
        raise HTTPException(status_code=404, detail="unknown workspace")


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    platform = get_platform()
    if platform is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    problems = await asyncio.to_thread(platform.validate)
    try:
        await asyncio.to_thread(platform.store.list_incidents, platform.workspaces.names()[0], limit=1)
    except Exception as exc:  # noqa: BLE001 - readiness reports, it does not raise
        problems.append(f"database: {type(exc).__name__}: {exc}")
    if problems:
        return JSONResponse({"status": "not_ready", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ready", "workspaces": platform.workspaces.names()})


@router.post("/ingest/{workspace}/{source}", status_code=202)
async def ingest_route(workspace: str, source: str, request: Request,
                       platform: PlatformDep) -> dict[str, Any]:
    body = await request.body()
    try:
        recorded = await asyncio.to_thread(
            ingest, platform, workspace, source, body, dict(request.headers),
            api_key=settings.api_key, auth_disabled=settings.api_auth_disabled,
        )
    except IngestFailure as failure:
        if failure.status_code == 401:
            logger.warning("rejected ingest for %s/%s: %s", workspace, source, failure.detail)
        raise HTTPException(status_code=failure.status_code, detail=failure.detail) from failure
    return {
        "accepted": len(recorded),
        "signals": [{"signal_id": r.signal_id, "incident_id": r.incident_id, "new_incident": r.created} for r in recorded],
    }


@router.get("/workspaces", dependencies=[Depends(require_api_key)])
async def workspaces(platform: PlatformDep) -> dict[str, Any]:
    return {
        "workspaces": [
            {
                "workspace": e.loaded.spec.workspace,
                "environment": e.loaded.spec.environment,
                "sources": [{"name": s.name, "type": s.type, "ingest_path": f"/v1/ingest/{e.loaded.spec.workspace}/{s.name}"
                             if s.runner is None else None, "runner": s.runner} for s in e.loaded.spec.sources],
                "channels": sorted(e.loaded.spec.channels),
            }
            for e in platform.workspaces.entries()
        ]
    }


@router.get("/workspaces/{workspace}/incidents", dependencies=[Depends(require_api_key)])
async def list_incidents(workspace: str, platform: PlatformDep, status: IncidentStatus | None = None,
                         limit: int = 50) -> dict[str, Any]:
    _workspace_or_404(platform, workspace)
    incidents = await asyncio.to_thread(platform.store.list_incidents, workspace, status=status, limit=limit)
    return {"incidents": [i.model_dump(mode="json") for i in incidents]}


@router.get("/workspaces/{workspace}/incidents/{incident_id}", dependencies=[Depends(require_api_key)])
async def get_incident(workspace: str, incident_id: str,
                       platform: PlatformDep) -> dict[str, Any]:
    _workspace_or_404(platform, workspace)
    incident = await asyncio.to_thread(platform.store.get, workspace, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="no incident with that id")
    events = await asyncio.to_thread(platform.store.events, workspace, incident_id)
    return {
        "incident": incident.model_dump(mode="json"),
        "timeline": [{"kind": e.kind, "actor": e.actor, "detail": e.detail, "at": e.created_at} for e in events],
    }
