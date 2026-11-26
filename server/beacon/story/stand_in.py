"""A stand-in for Ember that "chooses" the points of an incident's story, for running
with no key.

It is a responder for `EmberClient.fake`. It reads the request `merge_incident`
sends and answers with the form, filled in by rule from the lines it was given: the
newest radio line that says where the incident has got to, the lines that give the
alarm level and an evacuation, and the newest closure, aircraft, and taps line. It
understands nothing,
and what it chooses is a stand-in's: the story says `model == "fake"` and
`stand_in`. Code checks its points and writes the sentences, as it does for Ember's.
"""

import json
from collections.abc import Mapping
from typing import Any

from beacon.ember import fake_response
from beacon.incident.build import STATUS_WORDS


def stand_in_writer(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Answers `merge_incident` with points chosen by rule from the request's lines."""
    document = _document(body.get("input"))
    incident = document.get("incident") if isinstance(document.get("incident"), dict) else {}
    lines = [line for line in document.get("lines") or [] if isinstance(line, dict)]
    radio = [line for line in lines if line.get("kind") == "radio"]
    points: list[dict[str, Any]] = []
    # The newest call that says where the incident has got to: a call's line reads
    # `<what>: <status in words>.`.
    words = STATUS_WORDS.get(incident.get("status"))
    told = [line for line in radio if words and f": {words}." in str(line.get("text"))]
    if told:
        points.append({"kind": "status", "cites": [told[-1]["n"]]})
    alarm = incident.get("alarm")
    raised = [line for line in radio if f"Alarm level {alarm}." in str(line.get("text"))]
    if isinstance(alarm, int) and alarm >= 2 and raised:
        points.append({"kind": "alarm", "cites": [raised[0]["n"]]})
    # As the timeline's line says it (`beacon.incident.build.DETAIL_WORDS`).
    evacuating = [line for line in radio if "mention an evacuation" in str(line.get("text"))]
    if evacuating:
        points.append({"kind": "evacuation", "cites": [evacuating[0]["n"]]})
    for kind in ("closure", "aircraft", "taps"):
        seen = [line for line in lines if line.get("kind") == kind]
        if seen:
            points.append({"kind": kind, "cites": [seen[-1]["n"]]})
    return fake_response(text=json.dumps({"points": points}))


def _document(given: object) -> dict[str, Any]:
    """The JSON document a request's input is, or nothing for any other input."""
    if not isinstance(given, str):
        return {}
    try:
        document = json.loads(given)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}
