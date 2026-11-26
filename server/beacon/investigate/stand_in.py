"""A stand-in for Ember that "investigates" a tap, for running with no key.

It is a responder for `EmberClient.fake`. It reads the requests `investigate_tap`
sends and plays the investigator by rule: it asks what is near, takes as its lead
the incident that is going on with the most signals, and of those the nearest,
among the ones near enough to be matched by their place when there are any,
checks the lead's official sources, the aircraft, the other taps, and the clip or
photo when there is one, and answers with the grounds its own reading of the tools'
answers gives, or with "unconfirmed" when that gives none. It decides on crisis mode
when the lead has an alarm or an evacuation on the radio together with an official
alert or notice.

It understands nothing, and it hears and sees nothing: asked about a clip or a
photo it answers `none`. What it concludes is a stand-in's, and a run says so
(`model == "fake"`, `stand_in`). Code checks its answer as it checks Ember's.
"""

import json
from collections.abc import Mapping
from typing import Any

from beacon.ember import fake_response

# A distance comes rounded to 50 m, so this is the most that is surely within 500 m.
_NEAR_M = 450
_ON_RADIO = ("alarm", "evacuation_on_radio")
_OFFICIAL = ("official_alert", "official_notice")


def stand_in_investigator(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Answers the requests of one tap's investigation, by rule."""
    form = _format_name(body)
    if form == "tap_heard":
        return fake_response(text=json.dumps({"heard": ["none"]}))
    if form == "tap_seen":
        return fake_response(text=json.dumps({"seen": ["none"]}))
    offered = {tool.get("name") for tool in body.get("tools") or [] if isinstance(tool, dict)}
    returned = _returned(body.get("input"))
    if not returned and "nearby_incidents" in offered:
        return _calls("no_lead", None, "look_for_a_lead", [("nearby_incidents", _reach(1000, 30))])
    incidents = [
        incident
        for answer in returned
        for incident in answer.get("incidents") or []
        if isinstance(incident, dict) and isinstance(incident.get("id"), str)
    ]
    # An incident near enough to be matched by its place is taken before any that
    # is not, whatever signals a farther one has. They come nearest first, and
    # `max` keeps the first of those that tie.
    lead = max([i for i in incidents if _near(i)] or incidents, key=_weight, default=None)
    looked = any("taps" in answer for answer in returned)
    if lead is None:
        if not looked and "nearby_taps" in offered:
            calls = [("nearby_taps", _reach(500, 15)), ("get_aircraft", _reach(1000, 15))]
            return _calls("no_lead", None, "look_for_a_lead", calls)
        return _answer("unconfirmed", None, [], [])
    if not looked and "get_official" in offered:
        calls = [
            ("get_official", {"incident": lead["id"]}),
            ("get_aircraft", _reach(1000, 15)),
            ("nearby_taps", _reach(500, 15)),
        ]
        calls += [(name, {}) for name in ("listen_clip", "look_photo") if name in offered]
        return _calls("an_incident", lead["id"], "check_the_lead", calls)
    heard = {tag for answer in returned for tag in answer.get("heard") or []}
    seen = {tag for answer in returned for tag in answer.get("seen") or []}
    taps = max((answer.get("taps") or 0 for answer in returned if "taps" in answer), default=0)
    grounds = [
        ground
        for ground, holds in (
            ("near", _near(lead)),
            ("going_on", bool(lead.get("going_on"))),
            ("heard_in_clip", bool(heard & {"siren", "air_horn"})),
            ("seen_in_photo", bool(seen - {"none", "helicopter"})),
            ("aircraft_overhead", bool(lead.get("aircraft_over_it_in_reach_of_the_tap"))),
            ("other_taps", taps >= 1),
        )
        if holds
    ]
    if not grounds:
        # It found something, and nothing that ties this tap to it.
        return _answer("unconfirmed", None, [], [])
    signals = [kind for kind in lead.get("signals") or [] if isinstance(kind, str)]
    on_radio = [kind for kind in signals if kind in _ON_RADIO]
    official = [kind for kind in signals if kind in _OFFICIAL]
    crisis = [*on_radio, *official] if on_radio and official else []
    return _answer("incident", lead["id"], grounds, crisis)


def _near(incident: Mapping[str, Any]) -> bool:
    distance = incident.get("distance_m")
    return isinstance(distance, int) and distance <= _NEAR_M


def _weight(incident: Mapping[str, Any]) -> tuple[bool, int]:
    """What makes an incident the likelier lead: that it is going on, and then how
    many signals it has. A medical response is shown with none."""
    signals = incident.get("signals")
    return bool(incident.get("going_on")), len(signals) if isinstance(signals, list) else 0


def _format_name(body: Mapping[str, Any]) -> object:
    text = body.get("text")
    form = text.get("format") if isinstance(text, dict) else None
    return form.get("name") if isinstance(form, dict) else None


def _returned(given: object) -> list[dict[str, Any]]:
    """What the tools of the earlier rounds returned, in order."""
    answers = []
    for item in given if isinstance(given, list) else ():
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        try:
            answer = json.loads(item.get("output"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if isinstance(answer, dict):
            answers.append(answer)
    return answers


def _reach(radius_m: int, minutes: int) -> dict[str, Any]:
    return {"radius_m": radius_m, "minutes": minutes}


def _calls(
    thinking: str, lead: str | None, why: str, calls: list[tuple[str, dict[str, Any]]]
) -> Mapping[str, Any]:
    said = {"thinking": thinking, "lead": lead, "why": why}
    return fake_response(function_calls=[(name, said | args) for name, args in calls])


def _answer(
    outcome: str, incident: str | None, grounds: list[str], crisis_signals: list[str]
) -> Mapping[str, Any]:
    return fake_response(
        text=json.dumps(
            {
                "outcome": outcome,
                "incident": incident,
                "grounds": grounds,
                "crisis": bool(crisis_signals),
                "crisis_signals": crisis_signals,
            }
        )
    )
