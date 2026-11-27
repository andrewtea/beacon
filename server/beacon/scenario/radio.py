"""The radio of the mock scenario: invented dispatch calls, as a recorder would leave them.

Nothing here was ever said on a radio. A call is a tone, generated here, and what "was
said" in it is a form that a stand-in for Ember fills in when it is handed that tone
(`stand_in_ember`). So the real poller runs on this unchanged: it lists the calls
(`StoryShelf`, which stands where a directory of recordings would be), reads each
one's JSON and audio, and has Ember hear it. The Ember it has is the stand-in, and
the rows it stores say so (`model == "fake"`).

The calls tell the fire of `beacon.scenario.fire` from the radio's side, with what a
real feed would also carry: a medical call nearby and one across town, an alarm
elsewhere, a call nobody could make out, and a click of a microphone. Some of the
stand-in's forms are untidy in the ways a model's can be, with a house number where
the block belongs, details on a medical call, and a name that is no street, so that
what Beacon strips is seen to be stripped.

The system's name, the talkgroup numbers, and the frequency are invented too.
"""

import base64
import functools
import hashlib
import io
import json
import math
import wave
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from beacon.ember import fake_response
from beacon.ingest.recordings import Filed
from beacon.radio.vocabulary import Known

# What the story's recorder calls its system: the directory its calls would be in.
SYSTEM = "invented"
FIRE_DISPATCH = 9101
FIRE_TACTICAL = 9102
EMS = 9103
TALKGROUPS = (FIRE_DISPATCH, FIRE_TACTICAL, EMS)
EMS_TALKGROUPS = (EMS,)

# The streets and the units of the story, which stand where a list of the city's
# streets and the fire department's roster would for real radio. A name the stand-in
# gives that is not here is dropped, as an unlisted one is from a real call.
KNOWN = Known.of(
    streets=("Hayes St", "Gough St", "Fell St", "Geary Blvd", "Irving St"),
    units=(
        "B2", "B3", "D2", "E1", "E3", "E5", "E21", "E22", "E29", "E36",
        "M52", "M78", "RS1", "T1", "T3", "T6", "T12",
    ),
)  # fmt: skip

# The frequency the story's calls are on, which no system uses.
_FREQUENCY = 851000000

_SAMPLE_RATE = 8_000


@dataclass(frozen=True)
class RadioCall:
    # Minutes into the story.
    minute: float
    talkgroup: int
    seconds: float
    # The form the stand-in for Ember fills in when it hears the call. None for a
    # call nothing is made of.
    heard: dict[str, Any] | None = field(default=None, hash=False)


def _form(category: str, status: str = "unknown", **fields: Any) -> dict[str, Any]:
    return {
        "clarity": "clear",
        "category": category,
        "status": status,
        "details": [],
        "units": [],
        "alarm": None,
        "street": None,
        "block": None,
        "cross_street": None,
    } | fields


_HAYES = {"street": "Hayes St", "block": 400, "cross_street": "Gough St"}

CALLS: tuple[RadioCall, ...] = (
    RadioCall(
        0.0,
        FIRE_DISPATCH,
        6.0,
        _form("fire", "dispatched", units=["E36", "E21", "T6", "B2"], **_HAYES),
    ),
    RadioCall(0.6, FIRE_DISPATCH, 2.0, _form("fire", "en_route", units=["E36", "T6", "B2"])),
    RadioCall(
        3.0,
        FIRE_TACTICAL,
        4.0,
        # A model can give the house number it heard where the block belongs.
        _form(
            "fire", "on_scene", details=["smoke_showing"], units=["E36"], **_HAYES | {"block": 458}
        ),
    ),
    RadioCall(
        4.2,
        FIRE_TACTICAL,
        5.0,
        _form("fire", "working", details=["working_fire", "fire_showing"], units=["B2"], **_HAYES),
    ),
    # A click of a microphone: too short to be worth hearing, so it is never heard.
    RadioCall(5.0, FIRE_TACTICAL, 0.3),
    RadioCall(
        6.0,
        FIRE_DISPATCH,
        6.0,
        _form(
            "fire",
            "working",
            details=["additional_alarm"],
            units=["E5", "E3", "T3", "B3", "RS1"],
            alarm=2,
            **_HAYES,
        ),
    ),
    RadioCall(
        8.5,
        FIRE_DISPATCH,
        5.0,
        # A medical call on the fire channel, a block from the fire. The form has a
        # status, a detail, a house number, a reading where a unit belongs, and a
        # name where the cross street does, none of which is kept.
        _form(
            "medical",
            "on_scene",
            details=["rescue_in_progress"],
            units=["M52", "E21", "BP 180"],
            street="432 Fell Street",
            block=432,
            cross_street="Jane Doe St",
        ),
    ),
    RadioCall(
        12.0,
        FIRE_TACTICAL,
        5.0,
        _form(
            "fire",
            "working",
            details=["exposure_threatened", "police_requested", "road_closed"],
            units=["B2"],
            **_HAYES,
        ),
    ),
    RadioCall(
        14.0,
        FIRE_TACTICAL,
        4.0,
        _form("fire", "working", details=["evacuation"], units=["T6", "RS1"], **_HAYES),
    ),
    RadioCall(
        15.5,
        EMS,
        4.0,
        # On the EMS channel every call is a medical response, whatever is made of it.
        _form(
            "service",
            "en_route",
            details=["police_requested"],
            units=["M78"],
            street="Geary Blvd",
            block=5200,
        ),
    ),
    RadioCall(
        18.0,
        FIRE_DISPATCH,
        6.0,
        _form(
            "fire",
            "working",
            details=["additional_alarm"],
            units=["E1", "E29", "T1", "D2"],
            alarm=3,
            **_HAYES,
        ),
    ),
    # Wind across a microphone.
    RadioCall(21.0, FIRE_TACTICAL, 3.0, _form("unintelligible") | {"clarity": "unintelligible"}),
    RadioCall(
        23.0,
        FIRE_DISPATCH,
        4.0,
        # An alarm sounding across town, which has nothing to do with the fire.
        _form("alarm", "dispatched", units=["E22", "T12"], street="Irving St", block=1900),
    ),
    RadioCall(
        26.0,
        FIRE_TACTICAL,
        3.0,
        _form(
            "fire", "working", details=["aircraft_overhead", "utilities_requested"], units=["B2"]
        ),
    ),
    RadioCall(27.5, FIRE_DISPATCH, 3.0, _form("alarm", "cancelled", units=["E22", "T12"])),
    RadioCall(
        36.0,
        FIRE_TACTICAL,
        4.0,
        _form("fire", "working", details=["knocked_down"], units=["B2"], **_HAYES),
    ),
    RadioCall(
        46.0,
        FIRE_DISPATCH,
        5.0,
        _form("fire", "under_control", units=["B2"], alarm=3, **_HAYES),
    ),
    RadioCall(
        60.0,
        FIRE_TACTICAL,
        3.0,
        _form("fire", "under_control", details=["overhaul"], units=["E36", "T6"], **_HAYES),
    ),
    RadioCall(
        78.0,
        FIRE_DISPATCH,
        4.0,
        _form(
            "fire", "cleared", details=["all_clear"], units=["E5", "E3", "T3", "B3", "E1", "E29"]
        ),
    ),
    RadioCall(90.0, FIRE_DISPATCH, 3.0, _form("fire", "cleared", units=["E36", "T6", "B2"])),
)


class StoryShelf:
    """The story's calls, as a shelf of recordings lists them (`beacon.ingest.recordings`):
    by the moment `now()`, the calls that have been made, each with the JSON file
    trunk-recorder would have written for it and its tone. It is no directory, and
    so its calls are a stand-in's whoever reads them."""

    def __init__(self, start: datetime, now: Callable[[], datetime]) -> None:
        self._start = start
        self._now = now

    def listed(
        self, after_s: int, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        at = self._now()
        calls = [
            filed
            for index, call in enumerate(CALLS)
            if self._time(index) <= at
            and call.talkgroup in talkgroups
            and after_s < (filed := self._filed(index)).start_s <= upto_s
        ]
        calls.sort(key=lambda filed: (filed.start_s, filed.stem))
        return calls[:limit], len(calls) > limit

    def details(self, call: Filed) -> bytes | None:
        index = self._index(call)
        if index is None:
            return None
        made, start_s = CALLS[index], call.start_s
        return json.dumps(
            {
                "freq": _FREQUENCY,
                "start_time": start_s,
                "stop_time": start_s + math.ceil(made.seconds),
                "emergency": 0,
                "encrypted": 0,
                "call_length": made.seconds,
                "talkgroup": made.talkgroup,
                "talkgroup_tag": "",
                "audio_type": "digital",
                "short_name": SYSTEM,
                "freqList": [],
                "srcList": [],
            }
        ).encode()

    def audio(self, call: Filed) -> bytes | None:
        index = self._index(call)
        return None if index is None else _tone(index)

    def _time(self, index: int) -> datetime:
        return self._start + timedelta(minutes=CALLS[index].minute)

    def _filed(self, index: int) -> Filed:
        time = self._time(index)
        start_s = int(time.timestamp())
        return Filed(
            system=SYSTEM,
            day=(str(time.year), str(time.month), str(time.day)),
            stem=f"{CALLS[index].talkgroup}-{start_s}_{_FREQUENCY}-call_{index}",
            talkgroup=CALLS[index].talkgroup,
            start_s=start_s,
        )

    def _index(self, call: Filed) -> int | None:
        for index in range(len(CALLS)):
            if self._filed(index) == call:
                return index
        return None


@functools.cache
def _tone(index: int) -> bytes:
    """A WAV file for a call: a tone as long as the call, at a pitch of its own, so
    that no two calls have the same audio."""
    call = CALLS[index]
    period = _SAMPLE_RATE / (300 + 25 * index)
    samples = bytearray()
    for n in range(round(call.seconds * _SAMPLE_RATE)):
        level = round(6000 * math.sin(2 * math.pi * n / period))
        samples += level.to_bytes(2, "little", signed=True)
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(_SAMPLE_RATE)
        clip.writeframes(bytes(samples))
    return out.getvalue()


@functools.cache
def _heard_by_digest() -> dict[str, dict[str, Any]]:
    return {
        hashlib.sha256(_tone(index)).hexdigest(): call.heard
        for index, call in enumerate(CALLS)
        if call.heard is not None
    }


_NOTHING = _form("unintelligible") | {"clarity": "unintelligible"}


def stand_in_ember(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """A responder for `EmberClient.fake` that answers `extract_transmission` for the
    scenario's calls: it finds the audio in the request, and fills in the form the
    story has for that call. Any other audio is unintelligible to it. It hears
    nothing, and its answers are a stand-in's."""
    heard = _NOTHING
    for item in body.get("input") or []:
        for part in item.get("content") or [] if isinstance(item, Mapping) else []:
            audio = part.get("input_audio") if isinstance(part, Mapping) else None
            if isinstance(audio, Mapping) and isinstance(audio.get("data"), str):
                digest = hashlib.sha256(base64.b64decode(audio["data"])).hexdigest()
                heard = _heard_by_digest().get(digest, _NOTHING)
    return fake_response(text=json.dumps(heard))
