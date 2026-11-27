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
    # On a server that tells the invented scenario again when it is over
    # (`beacon.serve`, `BEACON_MOCK_REPEAT=1`): a sentence that says so, and what
    # becomes of the taps sent before. Shown with what says the scenario is
    # invented. Null on every other server.
    repeats_text: str | None = None
    # On a server reached through a host nobody has seen keep the private routes
    # out of its records (`BEACON_FRONT_END=unverified`): a sentence that says so,
    # shown wherever a person is about to send something. Null on every other.
    front_text: str | None = None
    # The rest of that: what is sent, what the host may keep, and what a tap's id
    # in a request's address lets its holder read. Shown when it is asked for,
    # beside the sentence above. Null whenever that one is.
    front_more_text: str | None = None


@router.get("/health")
def health(request: Request) -> Health:
    engine = getattr(request.app.state, "incidents", None)
    return Health(
        status="ok",
        version=__version__,
        mode=None if engine is None else engine.mode,
        repeats_text=getattr(request.app.state, "repeats_text", None),
        front_text=getattr(request.app.state, "front_text", None),
        front_more_text=getattr(request.app.state, "front_more_text", None),
    )
