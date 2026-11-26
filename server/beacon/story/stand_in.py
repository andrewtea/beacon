"""A stand-in for Ember that "writes" an incident's story, for running with no key.

It is a responder for `EmberClient.fake`. It reads the request `merge_incident`
sends and answers with the form, filled in by rule from the lines it was given: a
sentence for where the radio has got to, one for each kind of thing that was seen,
each citing the line it is from. It understands nothing, and what it writes is a
stand-in's: the story says `model == "fake"` and `stand_in`.
"""

import json
from collections.abc import Mapping
from typing import Any

from beacon.ember import fake_response

_STATUS = {
    "dispatched": "units were sent",
    "en_route": "units are on the way",
    "on_scene": "units are on scene",
    "working": "crews are at work",
    "under_control": "crews report it under control",
    "clearing": "units are clearing",
    "cleared": "units have cleared",
    "cancelled": "the call was cancelled",
}
_SEEN = {
    "closure": "511 lists a traffic event at this place.",
    "aircraft": "An aircraft was seen nearby.",
    "taps": "Taps came from nearby while this was going on.",
}


def stand_in_writer(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Answers `merge_incident` with a story made by rule from the request's lines."""
    document = _document(body.get("input"))
    incident = document.get("incident") if isinstance(document.get("incident"), dict) else {}
    lines = [line for line in document.get("lines") or [] if isinstance(line, dict)]
    radio = [line for line in lines if line.get("kind") == "radio"]
    claims: list[dict[str, Any]] = []
    if radio:
        said = _STATUS.get(incident.get("status"), "there is radio traffic")
        place = incident.get("place")
        where = f" at the {place}" if isinstance(place, str) and "block of" in place else ""
        claims.append(
            {"text": f"The radio says {said}{where}.", "cites": [radio[0]["n"], radio[-1]["n"]]}
        )
        alarm = incident.get("alarm")
        raised = [line for line in radio if f"Alarm level {alarm}." in str(line.get("text"))]
        if isinstance(alarm, int) and alarm >= 2 and raised:
            claims.append({"text": f"Crews report alarm level {alarm}.", "cites": [raised[0]["n"]]})
        evacuating = [line for line in radio if "mention an evacuation" in str(line.get("text"))]
        if evacuating:
            claims.append(
                {
                    "text": "Crews mention an evacuation on the radio.",
                    "cites": [evacuating[0]["n"]],
                }
            )
    for kind, text in _SEEN.items():
        seen = [line for line in lines if line.get("kind") == kind]
        if seen:
            claims.append({"text": text, "cites": [seen[-1]["n"]]})
    return fake_response(text=json.dumps({"headline": None, "claims": claims}))


def _document(given: object) -> dict[str, Any]:
    """The JSON document a request's input is, or nothing for any other input."""
    if not isinstance(given, str):
        return {}
    try:
        document = json.loads(given)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}
