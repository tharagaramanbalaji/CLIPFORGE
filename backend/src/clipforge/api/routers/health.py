from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from clipforge import __version__
from clipforge.api.deps import Probe, readiness_probes
from clipforge.log import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = __version__


class ReadyOut(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "fail"]]


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Liveness: the process is up. No external dependencies."""
    return HealthOut()


@router.get("/ready", response_model=ReadyOut)
def ready(response: Response, probes: Annotated[dict[str, Probe], Depends(readiness_probes)]) -> ReadyOut:
    """Readiness: database and broker reachable. Error details go to logs, not to clients."""
    checks: dict[str, Literal["ok", "fail"]] = {}
    for name, probe in probes.items():
        try:
            probe()
            checks[name] = "ok"
        except Exception as exc:
            log.warning("readiness_check_failed", check=name, error=str(exc))
            checks[name] = "fail"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyOut(status="ready" if ok else "not_ready", checks=checks)
