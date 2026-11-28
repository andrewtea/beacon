from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from beacon import __version__, front, telling

router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    # `mock` when Beacon is running on the invented scenario: whatever it shows is
    # then a stand-in's, and the app says so before anybody taps. Null when this
    # server reads no evidence.
    mode: Literal["live", "mock"] | None = None
    # On a server on the invented scenario: who does the model's work on it
    # (`beacon.telling`), a `stand_in` that works by rule or Muse Ember (`ember`).
    # Null when the server is on real sources or reads no evidence. Every incident,
    # story, and answer says of itself who made it; this says it of the server.
    teller: telling.Teller | None = None
    # Where Muse Ember works on the invented scenario: a sentence that says so, and
    # that the story is still made up. Shown with what says the scenario is
    # invented. Null on every other server.
    teller_text: str | None = None
    # Where a clip or a photo added to a tap can be sent to Meta, for Muse Ember
    # to say what is in it (`beacon.investigate.setup.MEDIA_SENT`): a sentence
    # that says so, shown where one is added. Null where the investigator is a
    # stand-in, where no tap is investigated, and where no clip or photo is taken.
    media_text: str | None = None
    # On a server that tells the invented scenario again when it is over
    # (`beacon.serve`, `BEACON_MOCK_REPEAT=1`): a sentence that says so, and what
    # becomes of the taps sent before. Shown with what says the scenario is
    # invented. Null on every other server.
    repeats_text: str | None = None
    # What this server takes from a visitor (`beacon.front`): their `own` place,
    # clip, photo, saved places, and notification address, or only the `invented`
    # scenario's two places, which is what a server takes that is reached through
    # a host nobody has seen keep the private routes out of its records
    # (`BEACON_FRONT_END=unverified`). The app then asks for no place, sound, or
    # photo and sends none.
    input: front.Input = "own"
    # On a server that takes only the invented places: the short of why, in a
    # sentence the app shows ahead of the controls that send something, on the
    # pages a person sends from. Null on every other server.
    front_line_text: str | None = None
    # On such a server: a sentence that says what it takes and why, shown on the
    # same pages, after those controls. Null on every other.
    front_text: str | None = None
    # The rest of that: what the host may keep, and what a tap's id in a request's
    # address lets its holder read. Shown when it is asked for, beside the
    # sentence above. Null whenever that one is.
    front_more_text: str | None = None


@router.get("/health")
def health(request: Request) -> Health:
    engine = getattr(request.app.state, "incidents", None)
    takes: front.Input = getattr(request.app.state, "input", "own")
    teller: telling.Teller | None = getattr(request.app.state, "teller", None)
    return Health(
        status="ok",
        version=__version__,
        mode=None if engine is None else engine.mode,
        teller=teller,
        teller_text=telling.TOLD_BY_EMBER if teller == "ember" else None,
        media_text=getattr(request.app.state, "media_text", None),
        repeats_text=getattr(request.app.state, "repeats_text", None),
        input=takes,
        front_line_text=front.INVENTED_LINE if takes == "invented" else None,
        front_text=front.INVENTED if takes == "invented" else None,
        front_more_text=front.INVENTED_MORE if takes == "invented" else None,
    )
