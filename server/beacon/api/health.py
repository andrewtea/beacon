from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from beacon import __version__

router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    # `mock` when Beacon is running on the invented scenario: whatever it shows is
    # then a stand-in's, and the app says so before anybody taps. Null when this
    # server reads no evidence.
    mode: Literal["live", "mock"] | None = None


@router.get("/health")
def health(request: Request) -> Health:
    engine = getattr(request.app.state, "incidents", None)
    return Health(status="ok", version=__version__, mode=None if engine is None else engine.mode)
