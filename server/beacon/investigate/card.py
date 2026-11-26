"""The words for an investigation: every sentence a person is shown about one.

A run is kept as ids, counts, and tags (`beacon.investigate.loop.Run`). What a person
reads is written here, by code, when the run is read: no sentence is kept, so there
is none that could be in yesterday's words, and none is a model's.

The sentences say what the investigator did and concluded, and call it that. A match
is "Beacon's investigator took this tap to be about this incident", which is a model's
reading of the evidence and is said to be one. None of them tells a reader what to
do: crisis mode is said to be on, and why, and that Beacon relays what authorities
publish.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from beacon.investigate.loop import Outcome, Run
from beacon.investigate.tools import Call

PENDING = "Beacon is looking into this tap."
MATCHED = (
    "Beacon's investigator took this tap to be about this incident. That is a model's "
    "reading of the evidence, not something a source said."
)
STAND_IN = (
    "A stand-in for Muse Ember made this investigation, by rule, and what it looked at "
    "may be the invented scenario's. No model investigated."
)
INCIDENT_GONE = (
    "Beacon no longer has the incident under the name it had when this tap was "
    "investigated: its calls were regrouped, or it is older than Beacon reads."
)
CRISIS = (
    "Beacon's investigator turned on crisis mode for this incident, because of what is "
    "listed here. That is a model's decision. Beacon relays what authorities publish "
    "and tells nobody what to do."
)
NEARBY = (
    "The investigator also found these near where the tap came from, and did not take "
    "the tap to be about them."
)
SINCE = (
    "This began near where the tap came from, after the tap. Beacon has not "
    "investigated whether it is what the tap was about."
)
_UNCONFIRMED = {
    "nothing_near": (
        "Unconfirmed. Beacon's investigator looked and found no incident near where this "
        "tap came from."
    ),
    "not_looked": (
        "Unconfirmed. Beacon's investigator answered without asking what is near where "
        "this tap came from."
    ),
    "not_sure": (
        "Unconfirmed. Beacon's investigator found incidents nearby and could not tell "
        "that this tap is about one of them."
    ),
    "not_grounded": (
        "Unconfirmed. The investigator named an incident, and what Beacon found did not "
        "bear the match out."
    ),
    "no_answer": "Unconfirmed. The investigator gave no answer Beacon could use.",
    "cut_short": "Unconfirmed. The investigation was cut short before it had an answer.",
    # True of a tap that was passed over, and of one whose run is not this server's
    # to read: it says what Beacon has, not what happened.
    "not_investigated": "Unconfirmed. Beacon has no investigation of this tap.",
}
_SOUNDS = {"siren": "a siren", "air_horn": "an air horn", "helicopter": "a helicopter"}
_SIGHTS = {
    "smoke": "smoke",
    "flames": "flames",
    "fire_apparatus": "a fire engine or truck",
    "ambulance": "an ambulance",
    "police_vehicle": "a police vehicle",
    "helicopter": "a helicopter",
    "road_blocked": "a blocked road",
}
_THINKING = {
    "no_lead": "No lead yet.",
    "an_incident": "Thinks it is an incident.",
    "an_aircraft": "Thinks it is an aircraft.",
    "nothing_here": "Thinks there is nothing here.",
}
_WHY = {
    "look_for_a_lead": "to look for a lead",
    "check_the_lead": "to check the lead",
    "rule_something_out": "to rule something out",
}
_REFUSED = {
    "no_such_tool": "Beacon ran nothing: the call named no tool it offers.",
    "bad_arguments": "Beacon ran nothing: an argument of the call was not on its list.",
    "no_such_incident": "Beacon no longer had that incident.",
    "no_clip": "The tap's clip was no longer kept.",
    "no_photo": "The tap's photo was no longer kept.",
}


def status_text(outcome: Outcome) -> str:
    """The sentence for how an investigation ended."""
    if outcome.status == "incident":
        return MATCHED
    return _UNCONFIRMED.get(outcome.reason or "", _UNCONFIRMED["cut_short"])


def ground_text(ground: str, run: Run, distance_m: float | None) -> str:
    """The sentence for one ground a match rests on, from what the run's tools
    returned. `distance_m` is how far the incident is from the tap now, or None
    when Beacon no longer has the incident."""
    if ground == "near":
        if distance_m is None:
            return "The incident was within 500 m of where the tap came from."
        return f"The incident is about {_metres(distance_m)} from where the tap came from."
    if ground == "going_on":
        return "The incident was going on when the tap came, or had been shortly before."
    if ground == "heard_in_clip":
        heard = _listed(_tags(run, "heard"), _SOUNDS)
        return f"In the clip sent with the tap, the investigator made out: {heard}."
    if ground == "seen_in_photo":
        seen = _listed(_tags(run, "seen"), _SIGHTS)
        return f"In the photo sent with the tap, the investigator made out: {seen}."
    if ground == "aircraft_overhead":
        return (
            "An aircraft was over the incident shortly before the tap, within reach of "
            "where the tap came from."
        )
    if ground == "other_taps":
        others = _taps(_most(run, "taps"))
        return f"{others} from nearby. Beacon cannot tell whether taps are from different people."
    return "The investigator gave a ground Beacon does not know."


def thinking_text(call: Call) -> str | None:
    """What the investigator said it was thinking with a call, or None."""
    return None if call.thinking is None else _THINKING.get(call.thinking)


def call_text(call: Call) -> str:
    """What a call asked for, and why."""
    args = call.args
    asked = {
        "nearby_incidents": "Asked which incidents are within {radius_m} m, going on or with "
        "a call in the last {minutes} min",
        "get_radio": "Asked for the radio calls of the last {minutes} min",
        "get_aircraft": "Asked for aircraft within {radius_m} m in the last {minutes} min",
        "get_official": "Asked for the official alerts, notices, and closures of its lead",
        "nearby_taps": "Asked how many other taps came from within {radius_m} m in the last "
        "{minutes} min",
        "listen_clip": "Had the tap's clip listened to",
        "look_photo": "Had the tap's photo looked at",
    }.get(call.tool)
    if asked is None or (
        not call.ok and not args and call.tool not in ("listen_clip", "look_photo")
    ):
        asked = "Made a call"
    else:
        asked = asked.format(**{"radius_m": "?", "minutes": "?"} | dict(args))
    why = "" if call.why is None else f", {_WHY[call.why]}"
    return f"{asked}{why}."


def returned_text(call: Call) -> str:
    """What a call came back with, in words made from its counts and tags."""
    got = call.returned
    if "error" in got:
        return _REFUSED.get(str(got["error"]), "Beacon ran nothing.")
    more = " There were more than Beacon read." if got.get("more") else ""
    if call.tool == "nearby_incidents":
        found = _count(len(got.get("incidents") or []), "incident")
        unlocated = got.get("unlocated") or 0
        aside = (
            f" {_count(unlocated, 'more incident')} going on had no known place, and "
            "could not be the answer."
            if unlocated
            else ""
        )
        return f"Found {found}.{aside}{more}"
    if call.tool == "get_radio":
        calls = _count(got.get("calls") or 0, "radio call")
        return f"Found {calls}, of {_count(len(got.get('incidents') or []), 'incident')}.{more}"
    if call.tool == "get_aircraft":
        return (
            f"Found {_count(got.get('tracks') or 0, 'aircraft', 'aircraft')}: "
            f"{got.get('circling') or 0} circling, {got.get('low') or 0} low.{more}"
        )
    if call.tool == "get_official":
        official = _count(
            got.get("official") or 0, "official alert or notice", "official alerts or notices"
        )
        return (
            f"Found {official}, {got.get('in_force') or 0} of them an alert in force, and "
            f"{_count(got.get('closures') or 0, 'road closure')} in force."
        )
    if call.tool == "nearby_taps":
        return f"Found {_count(got.get('taps') or 0, 'other tap')}.{more}"
    if call.tool == "listen_clip":
        return f"The investigator made out in the clip: {_listed(got.get('heard') or [], _SOUNDS)}."
    if call.tool == "look_photo":
        return f"The investigator made out in the photo: {_listed(got.get('seen') or [], _SIGHTS)}."
    return "Beacon ran nothing."


def cut_short_text(run: Run) -> str | None:
    """Why a run ended before its answer, or None when it did not."""
    if run.cut_short is None:
        return None
    if run.cut_short == "budget":
        return "The investigation reached the most one tap may cost, and stopped."
    if run.cut_short == "tool_failed":
        return (
            "Beacon could not read what one of its tools looks at, and the investigation stopped."
        )
    return "Muse Ember refused a call or failed on one, and the investigation stopped."


def _tags(run: Run, name: str) -> list[str]:
    """The tags a run's clip or photo call came back with."""
    for round_ in run.rounds:
        for call in round_.calls:
            if isinstance(call.returned.get(name), list):
                return [tag for tag in call.returned[name] if isinstance(tag, str)]
    return []


def _most(run: Run, name: str) -> int:
    counts = [
        call.returned[name]
        for round_ in run.rounds
        for call in round_.calls
        if call.tool == "nearby_taps" and isinstance(call.returned.get(name), int)
    ]
    return max(counts, default=0)


def _listed(tags: Sequence[Any], words: Mapping[str, str]) -> str:
    named = [words[tag] for tag in tags if tag in words]
    return ", ".join(named) if named else "nothing Beacon looks for"


def _taps(count: int) -> str:
    return f"{count} other tap came" if count == 1 else f"{count} other taps came"


def _count(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _metres(distance_m: float) -> str:
    return f"{max(50, int(round(distance_m / 50) * 50))} m"
