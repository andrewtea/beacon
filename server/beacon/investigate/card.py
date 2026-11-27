"""The words for an investigation: every sentence a person is shown about one.

A run is kept as ids, counts, and tags (`beacon.investigate.loop.Run`). What a person
reads is written here, by code, when the run is read: no sentence is kept, so there
is none that could be in yesterday's words, and none is a model's.

The sentences say what the investigator did and concluded, and call it that: a match
is "the investigator's reading of the evidence". Who the investigator was is said in
a sentence of its own (`investigator_text`): a model, whose reading can be wrong, or
a stand-in that works by rule. A stand-in is never called a model, and evidence
that is the invented scenario's is said to be that, whoever looked at it
(`stand_in_text`). None of the sentences tells a reader what to do: crisis mode is
said to be on, and that Beacon relays what authorities publish.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from beacon.investigate.loop import Outcome, Run
from beacon.investigate.tools import Call

PENDING = "Beacon is looking into this tap."
MATCHED = (
    "Beacon's investigator took this tap to be about this incident. That is the "
    "investigator's reading of the evidence, not something a source said."
)
# Who investigated, by the kind of Ember client (`Run.model`).
_INVESTIGATOR = {
    "live": "Beacon's investigator is a model, Muse Ember. Its reading can be wrong.",
    "fake": ("A stand-in for Muse Ember made this investigation, by rule. No model investigated."),
}
EVIDENCE_STAND_IN = (
    "What is shown here is built from Beacon's invented scenario, or from a stand-in's "
    "rows. It is not a record of anything that happened."
)
INCIDENT_GONE = (
    "Beacon no longer has the incident under the name it had when this tap was "
    "investigated: its calls were regrouped, or it is older than Beacon reads."
)
CRISIS = (
    "Beacon's investigator turned on crisis mode for this incident, because of the "
    "signals the incident had at that time. That is the investigator's decision, not "
    "an authority's. Beacon relays what authorities publish and tells nobody what to do."
)
# With the signals an incident has now, of the kinds a decision rested on.
CRISIS_NOW = (
    "These are the signals of those kinds the incident has now. They can rest on other "
    "rows than the ones the decision rested on."
)
CRISIS_NONE_NOW = "The incident has none of the signals now that the decision rested on."
NEARBY = (
    "The investigator also found these near where the tap came from, and did not take "
    "the tap to be about them."
)
SINCE = (
    "The first radio call of this incident, near where the tap came from, came after "
    "the tap. Nobody has investigated whether it is what the tap was about."
)
# Neither says where the rest is: Beacon does not know that there is a rest.
COST_INCOMPLETE = (
    "These totals may be incomplete: a call to Muse Ember ended in an error after it "
    "may have been sent, and may have been paid for."
)
COST_LOST = (
    "These totals may be incomplete: nothing was kept of this investigation, and it "
    "may have made calls to Muse Ember before it was lost."
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
    "no_answer": (
        "The investigator's answer was cut short, was not the form, or named nothing "
        "on Beacon's list, so nothing is known of what the clip or the photo holds."
    ),
}


def status_text(outcome: Outcome) -> str:
    """The sentence for how an investigation ended."""
    if outcome.status == "incident":
        return MATCHED
    return _UNCONFIRMED.get(outcome.reason or "", _UNCONFIRMED["cut_short"])


def investigator_text(model: str | None) -> str | None:
    """Who investigated, for a run made by that kind of Ember client. None when
    Ember was asked nothing."""
    if model is None:
        return None
    # A kind of client this Beacon does not know is not said to be a model.
    return _INVESTIGATOR.get(model, _INVESTIGATOR["fake"])


def stand_in_text(model: str | None, *, evidence: bool) -> str | None:
    """What says that a stand-in had a part in what is shown: that a stand-in
    investigated, when one did, and that what is shown is built from a stand-in's
    rows, when it is. Each is said only when it is so. None when neither is."""
    said = [_INVESTIGATOR["fake"]] if model is not None and model != "live" else []
    said += [EVIDENCE_STAND_IN] if evidence else []
    return " ".join(said) or None


# What each ground was checked against: a tool of the run, or the incident's own
# aircraft in the run's reading of the evidence.
GROUND_BASIS = {
    "near": "nearby_incidents",
    "going_on": "nearby_incidents",
    "heard_in_clip": "listen_clip",
    "seen_in_photo": "look_photo",
    "aircraft_overhead": "incident_aircraft",
    "other_taps": "nearby_taps",
}


def found_then(run: Run, incident_id: str | None) -> Mapping[str, Any] | None:
    """What the run's `nearby_incidents` kept of an incident: how far it was from
    the tap then, rounded, and where its point was from. None when it kept none."""
    for round_ in run.rounds:
        for call in round_.calls:
            if call.tool != "nearby_incidents":
                continue
            for item in call.returned.get("incidents") or []:
                if isinstance(item, Mapping) and item.get("id") == incident_id:
                    return item
    return None


def ground_feeds(ground: str, run: Run) -> list[str]:
    """The feeds with a credit of their own that a ground of the run's match rested
    on, by the names in `beacon.incident.build.CREDITS`: 511 for a distance measured
    from a closure's point, and for an aircraft, or a helicopter made out in a clip
    or a photo, the feed its positions were from. It is read from what the run's
    `nearby_incidents` kept then, and never worked out from the kind of ground: a
    run of the invented scenario rests on no feed's rows, and a run kept before
    this was written down names none."""
    then = found_then(run, run.outcome.incident_id) or {}
    if ground == "near":
        return ["511"] if then.get("point_from") == "closure" else []
    if ground == "aircraft_overhead":
        return _feeds(then.get("aircraft_credits"))
    made_out = {"heard_in_clip": "heard", "seen_in_photo": "seen"}.get(ground)
    if made_out is not None and "helicopter" in _tags(run, made_out):
        # The helicopter fits the incident only by a rotorcraft that was recorded
        # over it, so the ground rests on that aircraft's positions.
        return _feeds(then.get("rotorcraft_credits"))
    return []


def _feeds(kept: Any) -> list[str]:
    return [feed for feed in kept if isinstance(feed, str)] if isinstance(kept, list) else []


def ground_text(ground: str, run: Run) -> str:
    """The sentence for one ground a match rests on, from what the run's tools
    returned then. Nothing in it is from how the incident is now: an incident
    whose place has since moved is not said to have been where it is."""
    if ground == "near":
        then = found_then(run, run.outcome.incident_id)
        distance = None if then is None else then.get("distance_m")
        if not isinstance(distance, int) or isinstance(distance, bool):
            return "When Beacon looked, the incident was within 500 m of where the tap came from."
        return f"When Beacon looked, the incident was {_metres(distance)} where the tap came from."
    if ground == "going_on":
        # What code checked, and no more: an incident that is over can have an
        # update, such as a press release, and that is not its going on.
        return (
            "The incident was open when the tap came, or Beacon had an update about it "
            "in the fifteen minutes before."
        )
    if ground == "heard_in_clip":
        return (
            "In the clip sent with the tap, the investigator made out: "
            f"{_made_out(_tags(run, 'heard'), _SOUNDS)}"
        )
    if ground == "seen_in_photo":
        return (
            "In the photo sent with the tap, the investigator made out: "
            f"{_made_out(_tags(run, 'seen'), _SIGHTS)}"
        )
    if ground == "aircraft_overhead":
        return (
            "An aircraft that was over the incident was also within reach of where the "
            "tap came from, shortly before the tap."
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
        closures = got.get("closures") or 0
        listed = "lists" if closures == 1 else "list"
        # As the engine's closure signal has it: a road of the incident's, or a
        # road closed near its place.
        return (
            f"Found {official}, {got.get('in_force') or 0} of them an alert in force. "
            f"{_count(closures, '511 event')} in force {listed} a road as closed on a "
            "street of the incident's or near it."
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
        # It can have stopped well short of the cap: what could not fit was the
        # most its next call could have cost.
        return (
            "The investigation stopped: its next call to Muse Ember could have taken it "
            "past the most one tap may cost."
        )
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


def _made_out(tags: Sequence[Any], words: Mapping[str, str]) -> str:
    """What was made out in a clip or a photo, and how it bears on the match. The
    ground holds when one of them fits the incident, and that is all that is said:
    which of several did is not kept."""
    named = [words[tag] for tag in tags if tag in words]
    if len(named) == 1:
        return f"{named[0]}. That fits an incident of this kind."
    return f"{_listed(tags, words)}. At least one of these fits an incident of this kind."


def _taps(count: int) -> str:
    return f"{count} other tap came" if count == 1 else f"{count} other taps came"


def _count(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _metres(distance_m: int) -> str:
    """A distance a run kept, which is rounded to 50 m, with the word that goes
    before "where the tap came from". Zero is anything nearer than 25 m."""
    return "within 25 m of" if distance_m <= 0 else f"about {distance_m} m from"
