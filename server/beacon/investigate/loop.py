"""`investigate_tap`: Ember looks into one tap, and code checks what it concludes.

A tap says only that somebody noticed something. The investigator finds out what:
Ember is told that a tap came in, chooses which tools to call and with which
arguments (`beacon.investigate.tools`), and after at most `MAX_ROUNDS` rounds of
calls answers with the incident the tap is about, or with "unconfirmed".

What Ember decides, and what it does not:

- **Ember chooses where to look**, round by round, and says with each call what it
  is thinking and why it calls, from closed lists.
- **Ember answers with a form, not prose**: an incident's id or "unconfirmed", the
  grounds for the match from a closed list (`GROUNDS`), and whether the incident's
  signals add up to crisis mode, with the signals it rests that on. No word Ember
  writes is read, kept, or shown; whoever shows an answer words it from the form
  (`beacon.investigate.card`).
- **Code checks the answer.** The incident has to be one a tool returned in this
  run, which means it has a known place within reach of the tap. Each ground is
  checked against what the tools returned, and one that does not hold is dropped
  and counted. A match stands only when it is near (`near`), or on two other
  grounds. Otherwise the answer is "unconfirmed": thin evidence is not a guess.
- **Crisis mode is Ember's decision, on grounds code checks**: every signal it
  cites has to be one the incident engine found for that incident, and a medical
  response is never one. Beacon decides nothing else from it and tells nobody what
  to do.
- **How far to trust a match is code's to say** (`Outcome.confidence`): `medium`
  when the incident is close, going on, and rests on two grounds, else `low`. A
  tap is the least trusted evidence there is, and a match is never more than
  `medium`.

The run is bounded: `MAX_ROUNDS` rounds of tool calls, `MAX_CALLS_PER_ROUND` calls
in a round, and `TAP_BUDGET_USD` for all of a tap's calls, looked at before each
one. A run that is cut short, by a cap or by Ember failing, ends "unconfirmed".

Every call is `sensitive=True`: a request holds what sources said, as tags, and the
ledger keeps the accounting only. What is kept of a run is `Run`, which holds ids,
counts, tags, and each call's tokens, time, and cost.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from beacon.ember import EmberClient, EmberError
from beacon.incident import Incident
from beacon.incident.aircraft import NEAR_M as AIRCRAFT_NEAR_M
from beacon.investigate.tools import (
    SIGNAL_KINDS,
    Accounting,
    Call,
    Found,
    Scene,
    run_tool,
    tool_specs,
)
from beacon.radio.vocabulary import MEDICAL_EVENT

TASK = "investigate_tap"
# The most rounds of tool calls. One more request follows, which may only answer.
MAX_ROUNDS = 4
MAX_CALLS_PER_ROUND = 6
MAX_OUTPUT_TOKENS = 1200
# The most all of one tap's calls may cost. It is looked at before each call to
# Ember, a tool's own included, so a tap can go over it by one call and no more.
TAP_BUDGET_USD = 0.01

# What a match can rest on. Each is checked by `_holds`.
GROUNDS: tuple[str, ...] = (
    "near",
    "going_on",
    "heard_in_clip",
    "seen_in_photo",
    "aircraft_overhead",
    "other_taps",
)
# Why an answer is "unconfirmed".
UNCONFIRMED: tuple[str, ...] = (
    # Ember said so, and `nearby_incidents` had run and returned no incident.
    "nothing_near",
    # Ember said so, and had not asked what is near.
    "not_looked",
    # Ember said so, though a tool had returned one.
    "not_sure",
    # Ember named an incident, and the match did not stand.
    "not_grounded",
    # Ember's last answer was not the form.
    "no_answer",
    # The run ended before an answer: a cap, or Ember refused or failed.
    "cut_short",
    # No run was made: Beacon was not investigating taps, or not this one.
    "not_investigated",
)
# How near is near, and how near and well grounded a match is trusted more.
NEAR_M = 500.0
CLOSE_M = 250.0
# How long before a tap the last news of an incident may be for it to be going on.
RECENT = timedelta(minutes=15)

_HEARD = frozenset({"siren", "air_horn"})
_SEEN = frozenset(
    {"smoke", "flames", "fire_apparatus", "ambulance", "police_vehicle", "road_blocked"}
)

INSTRUCTIONS = """\
You are the investigator of Beacon, an app for San Francisco. A person heard or saw \
something, such as a siren or a helicopter, and pressed "What's that?". You find out \
which incident Beacon knows of the tap is about, if any.

You have tools. Each looks from where the tap came from; you are not told where that \
is and do not need it. Call nearby_incidents first. Then call what you need to check \
the lead or rule something out: the radio, aircraft, an incident's official alerts and \
closures, other taps, and the clip or photo the person sent, when there is one. With \
every call say what you are thinking, which incident is your lead (its id, or null), \
and why you call. You have at most four rounds of calls; use fewer when you know enough.

Then answer with the form:
- outcome: "incident" when the tap is about one of the incidents a tool returned, else \
"unconfirmed". Thin evidence is "unconfirmed". Never guess.
- incident: its id, or null.
- grounds: what the match rests on. near: it is within 500 m. going_on: it is going on \
or there was news of it in the last 15 minutes. heard_in_clip, seen_in_photo: the clip or \
photo fits it. aircraft_overhead: an aircraft was over it in the last 15 minutes, and the \
tap is in reach of it. other_taps: other taps came from nearby. Beacon checks each ground \
and throws away one that does not hold.
- crisis: true when the incident's signals add up to more than an everyday call, such \
as a second alarm together with an official alert or an evacuation. crisis_signals: the \
signals of the incident that you rest that on.

Write nothing else. Beacon writes every word a person is shown.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["incident", "unconfirmed"]},
        "incident": {"type": ["string", "null"]},
        "grounds": {"type": "array", "items": {"type": "string", "enum": list(GROUNDS)}},
        "crisis": {"type": "boolean"},
        "crisis_signals": {
            "type": "array",
            "items": {"type": "string", "enum": list(SIGNAL_KINDS)},
        },
    },
    "required": ["outcome", "incident", "grounds", "crisis", "crisis_signals"],
    "additionalProperties": False,
}
_FORMAT = {
    "format": {"type": "json_schema", "name": "tap_answer", "strict": True, "schema": SCHEMA}
}


@dataclass(frozen=True)
class Outcome:
    """What an investigation came to, after code checked Ember's answer."""

    status: Literal["incident", "unconfirmed"]
    # Why it is unconfirmed, from `UNCONFIRMED`. None for a match.
    reason: str | None = None
    incident_id: str | None = None
    # The grounds that held, in the order of `GROUNDS`, and how many Ember gave
    # that did not.
    grounds: tuple[str, ...] = ()
    grounds_dropped: int = 0
    # How far the match is trusted, which code works out. None when unconfirmed.
    confidence: Literal["medium", "low"] | None = None
    # Whether Ember decided on crisis mode and the decision stood, and the signals
    # of the incident it rests on. `crisis_refused` says Ember decided on it and
    # the decision did not stand.
    crisis: bool = False
    crisis_signals: tuple[str, ...] = ()
    crisis_refused: bool = False


@dataclass(frozen=True)
class Round:
    """One request to Ember that came back with tool calls, and the calls."""

    n: int
    ember: Accounting
    calls: tuple[Call, ...]


@dataclass(frozen=True)
class Run:
    """One tap's investigation, as it is kept. Every text in it is an id or from a
    closed list: it holds nothing a person sent and nothing a source said."""

    tap_id: str
    started_at: datetime
    finished_at: datetime
    outcome: Outcome
    rounds: tuple[Round, ...] = ()
    # The request that came back with the answer. None when there was none.
    final: Accounting | None = None
    # Which kind of Ember client investigated: `live`, or `fake` for a stand-in.
    # None when Ember was asked nothing.
    model: str | None = None
    # Whether a stand-in had a part in it: a fake Ember investigated, or what it
    # looked at is the invented scenario's.
    stand_in: bool = False
    # What cut the run short: the kind of error Ember raised, by its class's name,
    # `budget` for the tap's own cap, or `tool_failed` when a tool of Beacon's own
    # could not read what it looks at. None for a run that ran to its answer.
    cut_short: str | None = None
    # The reading of the evidence it looked at.
    as_of: datetime | None = None

    @property
    def calls(self) -> list[Accounting]:
        """Every call to Ember the run made, in order."""
        made: list[Accounting] = []
        for round_ in self.rounds:
            made.append(round_.ember)
            made += [call.ember for call in round_.calls if call.ember is not None]
        return made if self.final is None else [*made, self.final]

    @property
    def cost_usd(self) -> float:
        return sum(call.cost_usd for call in self.calls)


@dataclass(frozen=True)
class Investigation:
    """A run, and the error that ended it when Ember refused or failed, for the
    caller to hold its desk by. The error is not kept."""

    run: Run
    failure: EmberError | None = None
    # Whether Ember's last answer was a filled-in form, when it gave one.
    usable: bool = True


def investigate_tap(
    ember: EmberClient, scene: Scene, *, clock: Callable[[], datetime] | None = None
) -> Investigation:
    """Have Ember look into the tap of `scene`, and return the run. It does not
    raise what Ember raises: a run that Ember cut short ends "unconfirmed", with
    the error beside it."""
    clock = (lambda: datetime.now(UTC)) if clock is None else clock
    started = clock()
    found = Found()
    tools = tool_specs(scene)
    rounds: list[Round] = []
    request: list[Any] = [{"role": "user", "content": _opening(scene)}]
    spent = 0.0
    failure: EmberError | None = None
    cut_short: str | None = None
    final: Accounting | None = None
    said: object = None
    model: str | None = None
    for n in range(1, MAX_ROUNDS + 2):
        if spent >= TAP_BUDGET_USD:
            cut_short = "budget"
            break
        last = n > MAX_ROUNDS
        try:
            answer = ember.respond(
                task=TASK,
                effort="low",
                max_output_tokens=MAX_OUTPUT_TOKENS,
                sensitive=True,
                instructions=INSTRUCTIONS,
                input=request,
                # The last request offers no tool, so it can only answer.
                tools=None if last else tools,
                text=_FORMAT,
            )
        except EmberError as error:
            failure = error
        if failure is not None:
            break
        paid = Accounting.of(answer)
        model = answer.source
        spent += paid.cost_usd
        asked = [] if last else _function_calls(answer.output)
        if not asked:
            final = paid
            said = _form(answer.output_text)
            break
        calls: list[Call] = []
        outputs: list[dict[str, Any]] = []
        for place, (call_id, name, arguments) in enumerate(asked):
            if place >= MAX_CALLS_PER_ROUND:
                # Every call is answered, so the next request is one Ember accepts.
                told: dict[str, Any] = {"error": "too_many_calls"}
            else:
                if spent >= TAP_BUDGET_USD:
                    cut_short = "budget"
                    break
                try:
                    call, told = run_tool(ember, scene, found, name, arguments)
                except EmberError as error:
                    failure = error
                except Exception:
                    # A tool of Beacon's own failed: the store or the tap's media
                    # could not be read. What was paid for so far is kept with the
                    # run, and nothing of the error is, which can name a file.
                    cut_short = "tool_failed"
                if failure is not None or cut_short is not None:
                    break
                calls.append(call)
                spent += 0.0 if call.ember is None else call.ember.cost_usd
            outputs.append(
                {"type": "function_call_output", "call_id": call_id, "output": json.dumps(told)}
            )
        rounds.append(Round(n=n, ember=paid, calls=tuple(calls)))
        if failure is not None or cut_short is not None:
            break
        request = [*request, *answer.output, *outputs]
    if failure is not None:
        cut_short = type(failure).__name__
    if cut_short is not None:
        outcome = Outcome(status="unconfirmed", reason="cut_short")
    else:
        outcome = _outcome(said, scene, found)
    matched = None if outcome.incident_id is None else scene.snapshot.get(outcome.incident_id)
    run = Run(
        tap_id=scene.tap_id,
        started_at=started,
        finished_at=clock(),
        outcome=outcome,
        rounds=tuple(rounds),
        final=final,
        model=model,
        stand_in=(model is not None and model != "live")
        or scene.store.mode == "mock"
        or (matched is not None and matched.stand_in),
        cut_short=cut_short,
        as_of=scene.snapshot.as_of,
    )
    return Investigation(run=run, failure=failure, usable=outcome.reason != "no_answer")


def _opening(scene: Scene) -> str:
    """What Ember is told of the tap: that it came, and what was sent with it.
    Not where it came from."""
    return json.dumps(
        {
            "tap": {
                "seconds_ago": max(0, round((scene.snapshot.as_of - scene.at).total_seconds())),
                "sent_a_clip": scene.has_clip,
                "sent_a_photo": scene.has_photo,
            }
        },
        sort_keys=True,
    )


def _function_calls(output: Sequence[object]) -> list[tuple[str, object, object]]:
    """The function calls of an answer: each one's id, name, and arguments."""
    return [
        (item["call_id"], item.get("name"), item.get("arguments"))
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and isinstance(item.get("call_id"), str)
    ]


def _form(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _outcome(said: object, scene: Scene, found: Found) -> Outcome:
    """What stands of Ember's answer. Nothing is read of it but the form's fields."""
    if not isinstance(said, Mapping) or said.get("outcome") not in ("incident", "unconfirmed"):
        return Outcome(status="unconfirmed", reason="no_answer")
    if said["outcome"] == "unconfirmed":
        # What is said of an unconfirmed tap is what the run's tools showed, and
        # not Ember's word for it.
        looked = "nothing_near" if found.looked else "not_looked"
        return Outcome(status="unconfirmed", reason="not_sure" if found.incidents else looked)
    named = said.get("incident")
    # Only an incident a tool of this run returned, which is in this reading.
    incident = scene.snapshot.get(named) if isinstance(named, str) else None
    if incident is None or incident.id not in found.incidents:
        return Outcome(status="unconfirmed", reason="not_grounded")
    distance = found.incidents[incident.id]
    given = said.get("grounds")
    cited = (
        {ground for ground in given if isinstance(ground, str)}
        if isinstance(given, list)
        else set()
    )
    held = tuple(
        ground
        for ground in GROUNDS
        if ground in cited and _holds(ground, incident, distance, scene, found)
    )
    dropped = len(cited) - len(held)
    if "near" not in held and len(held) < 2:
        return Outcome(status="unconfirmed", reason="not_grounded", grounds_dropped=dropped)
    close = distance <= CLOSE_M and {"near", "going_on"} <= set(held)
    crisis, signals, refused = _crisis(said, incident)
    return Outcome(
        status="incident",
        incident_id=incident.id,
        grounds=held,
        grounds_dropped=dropped,
        confidence="medium" if close else "low",
        crisis=crisis,
        crisis_signals=signals,
        crisis_refused=refused,
    )


def _holds(ground: str, incident: Incident, distance: float, scene: Scene, found: Found) -> bool:
    """Whether a ground for matching the tap to `incident` is true of what the
    run's tools returned."""
    if ground == "near":
        return distance <= NEAR_M
    if ground == "going_on":
        return incident.open or incident.updated_at >= scene.at - RECENT
    # An aircraft that circled or came low over the incident, and was last seen
    # there shortly before the tap: one from an hour ago is not what was heard.
    helicopter = any(track.last_seen >= scene.at - RECENT for track in incident.aircraft)
    if ground == "heard_in_clip":
        heard = set(found.heard or ())
        return bool(heard & _HEARD) or ("helicopter" in heard and helicopter)
    if ground == "seen_in_photo":
        seen = set(found.seen or ())
        return bool(seen & _SEEN) or ("helicopter" in seen and helicopter)
    if ground == "aircraft_overhead":
        # Such an aircraft over the incident, and the tap within what counts as
        # under it or with an aircraft of its own.
        return helicopter and (found.aircraft or distance <= AIRCRAFT_NEAR_M)
    if ground == "other_taps":
        return found.other_taps >= 1
    return False


def _crisis(said: Mapping[str, Any], incident: Incident) -> tuple[bool, tuple[str, ...], bool]:
    """Whether Ember's decision on crisis mode stands, the signals it rests on,
    and whether it decided on it and the decision did not stand. Every signal it
    cites has to be one the engine found for the incident."""
    if said.get("crisis") is not True:
        return False, (), False
    given = said.get("crisis_signals")
    cited = {kind for kind in given if isinstance(kind, str)} if isinstance(given, list) else set()
    has = [signal.kind for signal in incident.signals]
    if incident.category == MEDICAL_EVENT or not cited or not cited <= set(has):
        return False, (), True
    return True, tuple(dict.fromkeys(kind for kind in has if kind in cited)), False
