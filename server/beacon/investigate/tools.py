"""The investigator's tools: what Ember may look at for one tap, and what it is shown.

Every tool is plain code over one reading of the incident engine and the evidence
store. Ember chooses which to call and with which arguments; it decides nothing a
tool returns.

- **A tool looks from where the tap is, and Ember is never told where that is.** No
  tool takes a place. A radius and a number of minutes are chosen from short lists
  (`RADII_M`, `MINUTES`), and code measures from the tap's own point, which is
  already rounded to about a block. So the request holds no position of a person,
  and Ember cannot look anywhere but around the tap.
- **A tool returns fields, not sentences.** Ids, counts, tags from closed lists, and
  numbers rounded so they say no more than is needed. Nothing an authority wrote
  and nothing a source said in its own words goes to Ember here: an alert is that
  there is one and how it stands, and a notice is its type from Beacon's own list.
  A field of a row that should hold a tag is told only when it is on its list
  (`_tag`), so a row that holds a sentence there tells Ember nothing.
- **A medical response is its category and how far away it is**, and nothing else.
- **What is kept of a call is smaller still** (`Call.returned`): ids, counts, and
  tags. That is what the trace of a run is made of.
- **The photo and the clip are looked at once each, by a call of their own**
  (`listen_clip`, `look_photo`), which is `sensitive=True` because a clip can hold
  a voice and a photo a face. Ember answers with tags from `SOUNDS` and `SIGHTS`,
  and only the tags are kept. There is no other way to a tap's photo: a photo adds
  evidence to a tap's investigation and is analyzed for nothing else.
"""

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, get_args

from beacon.ember import EmberClient, EmberResponse
from beacon.evidence import EvidenceStore, Point
from beacon.incident import Incident, Snapshot
from beacon.incident.aircraft import tracks
from beacon.incident.model import SignalKind
from beacon.ingest.opensky import AIRCRAFT_CATEGORIES
from beacon.notice import NOTICE_TYPES
from beacon.radio.vocabulary import CATEGORIES, DETAILS, MEDICAL_EVENT, STATUSES

# What Ember says it thinks the tap is about, with each call it makes.
THINKING: tuple[str, ...] = ("no_lead", "an_incident", "an_aircraft", "nothing_here")
# Why it makes a call.
WHY: tuple[str, ...] = ("look_for_a_lead", "check_the_lead", "rule_something_out")
# How far from the tap a tool may look, in metres, and how far back, in minutes.
RADII_M: tuple[int, ...] = (250, 500, 1000, 2000)
MINUTES: tuple[int, ...] = (5, 15, 30, 60)
# What can be said to be heard in a clip, and seen in a photo. `none` stands alone.
SOUNDS: tuple[str, ...] = ("siren", "air_horn", "helicopter", "none")
SIGHTS: tuple[str, ...] = (
    "smoke",
    "flames",
    "fire_apparatus",
    "ambulance",
    "police_vehicle",
    "helicopter",
    "road_blocked",
    "none",
)
SIGNAL_KINDS: tuple[str, ...] = get_args(SignalKind)

TOOL_NAMES: tuple[str, ...] = (
    "nearby_incidents",
    "get_radio",
    "get_aircraft",
    "get_official",
    "nearby_taps",
    "listen_clip",
    "look_photo",
)

# The most incidents, radio calls, and aircraft one call returns.
MAX_INCIDENTS = 8
MAX_CALLS = 40
MAX_TRACKS = 8
# The most rows of taps and of aircraft positions one call reads.
MAX_ROWS = 2_000
MEDIA_OUTPUT_TOKENS = 200
_ROUND_M = 50
_EVENTS = (*CATEGORIES, MEDICAL_EVENT)
_CLARITIES = ("clear", "partial", "unintelligible")


_CLIP_QUESTION = (
    "This is a few seconds a person recorded on a street in San Francisco when they "
    "pressed a button to ask what is going on nearby. Say which of these can be heard: "
    "a siren, an air horn, a helicopter. Answer none when none of them can. Say nothing "
    "of any voice or of anything anybody says."
)
_PHOTO_QUESTION = (
    "This is a photo a person took on a street in San Francisco when they pressed a "
    "button to ask what is going on nearby. Say which of these can be seen: smoke, "
    "flames, a fire engine or truck, an ambulance, a police vehicle, a helicopter, a "
    "blocked road. Answer none when none of them can. Say nothing of any person."
)


@dataclass(frozen=True)
class Scene:
    """What one investigation looks at: a tap, and one reading of everything else."""

    tap_id: str
    at: datetime
    # The tap's point as it is stored, which is rounded to about a block.
    point: Point
    has_photo: bool
    has_clip: bool
    snapshot: Snapshot
    store: EvidenceStore
    # A tap's photo or clip, or None when its hour is over (`Taps.media`).
    media: Callable[[str, str], bytes | None]


@dataclass
class Found:
    """What a run's tools have returned so far, which its answer is checked against."""

    # The incidents `nearby_incidents` returned, each with how far it is from the
    # tap, in metres. An answer can name no other incident.
    incidents: dict[str, float] = field(default_factory=dict)
    # Whether `nearby_incidents` ran at all: only then is "nothing near" known.
    looked: bool = False
    # Whether an aircraft that circled or came low was within reach of the tap.
    aircraft: bool = False
    other_taps: int = 0
    # None until the clip was listened to and the photo looked at.
    heard: tuple[str, ...] | None = None
    seen: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Accounting:
    """What one call to Ember cost, and where its row is in the ledger."""

    source: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int
    # The ledger row: its file's generation, its id, and its own token
    # (`EmberClient.ledger_calls`).
    ledger: tuple[str, int, str]

    @classmethod
    def of(cls, answer: EmberResponse) -> "Accounting":
        ref = answer.ledger_ref
        return cls(
            source=answer.source,
            input_tokens=answer.usage.input_tokens,
            cached_tokens=answer.usage.cached_tokens,
            output_tokens=answer.usage.output_tokens,
            reasoning_tokens=answer.usage.reasoning_tokens,
            cost_usd=answer.cost_usd,
            latency_ms=answer.latency_ms,
            ledger=(str(ref[0]), int(ref[1]), str(ref[2])),
        )


@dataclass(frozen=True)
class Call:
    """One call of a tool, as it is kept: what was asked, and what came back as
    ids, counts, and tags. Every text in it is from a closed list or is an id."""

    # From `TOOL_NAMES`, or `unknown` for a name that is not a tool's.
    tool: str
    # What Ember said it was thinking and why it called, or None for what was not
    # from the lists. `lead` is an incident a tool of this run had returned.
    thinking: str | None
    lead: str | None
    why: str | None
    # The arguments that were from the lists.
    args: Mapping[str, int | str]
    # Whether the tool ran. A call that names no tool, or gives an argument that is
    # not on its list, is refused, and Ember is told so.
    ok: bool
    returned: Mapping[str, Any]
    # For `listen_clip` and `look_photo`, the Ember call that was made for it.
    ember: Accounting | None = None


def tool_specs(scene: Scene) -> list[dict[str, Any]]:
    """The function tools a run offers Ember. The clip and the photo are offered
    only for a tap that sent one."""
    names = [
        name
        for name in TOOL_NAMES
        if (name != "listen_clip" or scene.has_clip) and (name != "look_photo" or scene.has_photo)
    ]
    return [
        {
            "type": "function",
            "name": name,
            "description": _DESCRIPTIONS[name],
            "strict": True,
            "parameters": _parameters(name),
        }
        for name in names
    ]


_DESCRIPTIONS = {
    "nearby_incidents": (
        "The incidents Beacon knows of near where the tap came from: each one's id, "
        "what it is, how it stands, and how far away it is. Call this first."
    ),
    "get_radio": (
        "The dispatch radio calls of the last minutes, as tags, each with the "
        "incident it belongs to."
    ),
    "get_aircraft": "Aircraft near where the tap came from, and whether one circled or came low.",
    "get_official": (
        "The official alerts, notices, and road closures of one incident, as their "
        "types and how they stand, and what makes the incident more than an everyday call."
    ),
    "nearby_taps": "How many other taps came from nearby. Taps, not people.",
    "listen_clip": "What can be heard in the few seconds the person recorded with the tap.",
    "look_photo": "What can be seen in the photo the person took with the tap.",
}
_EXTRA: dict[str, dict[str, Any]] = {
    "nearby_incidents": {
        "radius_m": {"type": "integer", "enum": list(RADII_M)},
        "minutes": {"type": "integer", "enum": list(MINUTES)},
    },
    "get_radio": {"minutes": {"type": "integer", "enum": list(MINUTES)}},
    "get_aircraft": {
        "radius_m": {"type": "integer", "enum": list(RADII_M)},
        "minutes": {"type": "integer", "enum": list(MINUTES)},
    },
    "get_official": {"incident": {"type": "string"}},
    "nearby_taps": {
        "radius_m": {"type": "integer", "enum": list(RADII_M)},
        "minutes": {"type": "integer", "enum": list(MINUTES)},
    },
    "listen_clip": {},
    "look_photo": {},
}


def _parameters(name: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "thinking": {"type": "string", "enum": list(THINKING)},
        "lead": {"type": ["string", "null"]},
        "why": {"type": "string", "enum": list(WHY)},
        **_EXTRA[name],
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def run_tool(
    ember: EmberClient, scene: Scene, found: Found, name: object, arguments: object
) -> tuple[Call, dict[str, Any]]:
    """Run one call Ember made, and return what is kept of it and what Ember is
    told. A call that is not one of the tools, or whose arguments are not from the
    lists, runs nothing.

    Raises what `EmberClient.respond` raises, for the clip and the photo."""
    given = _arguments(arguments)
    thinking = given.get("thinking") if given.get("thinking") in THINKING else None
    named = given.get("lead")
    lead = named if isinstance(named, str) and named in found.incidents else None
    why = given.get("why") if given.get("why") in WHY else None
    offered = {spec["name"] for spec in tool_specs(scene)}
    if not isinstance(name, str) or name not in offered:
        call = Call("unknown", thinking, lead, why, {}, False, {"error": "no_such_tool"})
        return call, {"error": "no_such_tool"}
    args = _checked(name, given, found)
    if args is None:
        call = Call(name, thinking, lead, why, {}, False, {"error": "bad_arguments"})
        return call, {"error": "bad_arguments"}
    told, kept, paid = _TOOLS[name](ember, scene, found, args)
    return Call(name, thinking, lead, why, args, "error" not in kept, kept, paid), told


def _arguments(arguments: object) -> dict[str, Any]:
    """A call's arguments as a mapping, or an empty one for what is not JSON."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return dict(arguments) if isinstance(arguments, Mapping) else {}


def _checked(name: str, given: Mapping[str, Any], found: Found) -> dict[str, int | str] | None:
    """A call's own arguments, when each is on its list. Else None."""
    args: dict[str, int | str] = {}
    for key, schema in _EXTRA[name].items():
        value = given.get(key)
        if key == "incident":
            # Only an incident a tool of this run returned.
            if not isinstance(value, str) or value not in found.incidents:
                return None
        elif isinstance(value, bool) or value not in schema["enum"]:
            return None
        args[key] = value
    return args


_Result = tuple[dict[str, Any], dict[str, Any], Accounting | None]


def _nearby_incidents(
    _ember: EmberClient, scene: Scene, found: Found, args: Mapping[str, Any]
) -> _Result:
    since = scene.at - timedelta(minutes=args["minutes"])
    near: list[tuple[float, Incident]] = []
    unlocated = 0
    for incident in scene.snapshot.incidents:
        if not (incident.open or incident.updated_at >= since):
            continue
        if incident.point is None:
            # It cannot be said to be near, so it cannot be the answer.
            unlocated += 1
            continue
        distance = incident.point.distance_m(scene.point)
        if distance <= args["radius_m"]:
            near.append((distance, incident))
    near.sort(key=lambda pair: (pair[0], pair[1].id))
    more = len(near) > MAX_INCIDENTS
    near = near[:MAX_INCIDENTS]
    found.looked = True
    for distance, incident in near:
        found.incidents[incident.id] = distance
    told = {
        "incidents": [_incident_told(scene, incident, distance) for distance, incident in near],
        "more": more,
        "going_on_with_no_known_place": unlocated,
    }
    kept = {
        "incidents": [incident.id for _, incident in near],
        "more": more,
        "unlocated": unlocated,
    }
    return told, kept, None


def _incident_told(scene: Scene, incident: Incident, distance: float) -> dict[str, Any]:
    told: dict[str, Any] = {
        "id": incident.id,
        "category": _tag(incident.category, _EVENTS),
        "distance_m": _rounded(distance),
        "going_on": incident.open,
    }
    if incident.category == MEDICAL_EVENT:
        # Its category and how far away it is, and no more.
        return told
    return told | {
        "status": _tag(incident.status, STATUSES),
        "alarm": _alarm(incident.alarm),
        "units": len(incident.units),
        "minutes_since_it_began": _minutes(scene.snapshot.as_of - incident.opened_at),
        "minutes_since_last_news": _minutes(scene.snapshot.as_of - incident.updated_at),
        "signals": [signal.kind for signal in incident.signals],
        "aircraft": len(incident.aircraft),
        # Not its taps: the tap that is being looked into can be one of them, and
        # `nearby_taps` counts the others.
    }


def _get_radio(
    _ember: EmberClient, scene: Scene, _found: Found, args: Mapping[str, Any]
) -> _Result:
    rows = scene.store.search(
        kinds=["radio"],
        start=scene.at - timedelta(minutes=args["minutes"]),
        end=scene.snapshot.as_of,
        up_to_id=scene.snapshot.last_id,
        newest_first=True,
        limit=MAX_CALLS + 1,
    )
    more = len(rows) > MAX_CALLS
    rows = rows[:MAX_CALLS]
    of = {
        evidence_id: incident.id
        for incident in scene.snapshot.incidents
        for evidence_id in incident.evidence_ids
    }
    calls = []
    for row in rows:
        call: dict[str, Any] = {
            "minutes_ago": _minutes(scene.snapshot.as_of - row.occurred_at),
            "incident": of.get(row.id),
            "event": _tag(row.data.get("event"), _EVENTS),
        }
        # Only what is known not to be a medical call is shown with more.
        if call["event"] not in (None, MEDICAL_EVENT):
            details, units = row.data.get("details"), row.data.get("units")
            call |= {
                "status": _tag(row.data.get("status"), STATUSES),
                "details": [
                    tag for tag in (details if isinstance(details, list) else []) if tag in DETAILS
                ],
                "units": len(units) if isinstance(units, list) else 0,
                "alarm": _alarm(row.data.get("alarm")),
                "clarity": _tag(row.data.get("clarity"), _CLARITIES),
            }
        calls.append(call)
    incidents = sorted({of[row.id] for row in rows if row.id in of})
    return (
        {"calls": calls, "more": more},
        {"calls": len(rows), "incidents": incidents, "more": more},
        None,
    )


def _get_aircraft(
    _ember: EmberClient, scene: Scene, found: Found, args: Mapping[str, Any]
) -> _Result:
    rows = scene.store.search(
        kinds=["aircraft"],
        start=scene.at - timedelta(minutes=args["minutes"]),
        end=scene.snapshot.as_of,
        near=scene.point,
        radius_m=float(args["radius_m"]),
        up_to_id=scene.snapshot.last_id,
        newest_first=True,
        limit=MAX_ROWS + 1,
    )
    more = len(rows) > MAX_ROWS
    seen = tracks(rows[:MAX_ROWS], scene.point, near_m=float(args["radius_m"]))
    seen.sort(key=lambda track: (not track.circling, not track.low, track.nearest_m))
    shown = seen[:MAX_TRACKS]
    found.aircraft = found.aircraft or any(track.circling or track.low for track in seen)
    told = {
        "aircraft": [
            {
                "category": _tag(track.category, AIRCRAFT_CATEGORIES),
                "minutes_nearby": _minutes(track.last_seen - track.first_seen),
                "minutes_since_last_seen": _minutes(scene.snapshot.as_of - track.last_seen),
                "nearest_m": _rounded(track.nearest_m),
                "lowest_m": None if track.lowest_m is None else _rounded(track.lowest_m),
                "circling": track.circling,
                "low": track.low,
            }
            for track in shown
        ],
        "more": more or len(seen) > MAX_TRACKS,
    }
    kept = {
        "tracks": len(seen),
        "circling": sum(track.circling for track in seen),
        "low": sum(track.low for track in seen),
        "more": more,
    }
    return told, kept, None


def _get_official(
    _ember: EmberClient, scene: Scene, _found: Found, args: Mapping[str, Any]
) -> _Result:
    incident = scene.snapshot.get(str(args["incident"]))
    if incident is None:
        return {"error": "no_such_incident"}, {"error": "no_such_incident"}, None
    closures = [
        entry
        for entry in incident.timeline
        if entry.kind == "closure" and entry.details.get("state") == "in_force"
    ]
    told = {
        "incident": incident.id,
        # That there is one and how it stands, and for a notice its type, which is
        # from Beacon's own list. An alert's type is the authority's own word for
        # it, and is not told.
        "official": [
            {
                "kind": item.kind,
                "type": _tag(item.type, NOTICE_TYPES) if item.kind == "notice" else None,
                "state": item.state,
                "minutes_since_issued": _minutes(scene.snapshot.as_of - item.issued_at),
            }
            for item in incident.official
        ],
        "road_closures_in_force": len(closures),
        "radio_and_official_disagree": [conflict.kind for conflict in incident.conflicts],
        "signals": [signal.kind for signal in incident.signals],
    }
    kept = {
        "incident": incident.id,
        "official": len(incident.official),
        "in_force": sum(item.state == "in_force" for item in incident.official),
        "closures": len(closures),
        "conflicts": [conflict.kind for conflict in incident.conflicts],
        "signals": [signal.kind for signal in incident.signals],
    }
    return told, kept, None


def _nearby_taps(
    _ember: EmberClient, scene: Scene, found: Found, args: Mapping[str, Any]
) -> _Result:
    rows = scene.store.search(
        kinds=["tap"],
        start=scene.at - timedelta(minutes=args["minutes"]),
        end=scene.snapshot.as_of,
        near=scene.point,
        radius_m=float(args["radius_m"]),
        up_to_id=scene.snapshot.last_id,
        newest_first=True,
        limit=MAX_ROWS + 1,
    )
    more = len(rows) > MAX_ROWS
    # The tap that is being looked into is not another tap.
    others = sum(row.ref != scene.tap_id for row in rows[:MAX_ROWS])
    found.other_taps = max(found.other_taps, others)
    kept = {"taps": others, "more": more}
    return kept | {"note": "Taps, not people: nothing tells one sender from another."}, kept, None


def _listen_clip(
    ember: EmberClient, scene: Scene, found: Found, _args: Mapping[str, Any]
) -> _Result:
    if found.heard is not None:
        # Listened to once, and paid for once.
        kept = {"heard": list(found.heard), "again": True}
        return kept, kept, None
    clip = scene.media(scene.tap_id, "clip")
    if clip is None:
        return {"error": "no_clip"}, {"error": "no_clip"}, None
    content = {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(clip).decode(), "format": "wav"},
    }
    tags, paid = _media_tags(ember, "listen_clip", _CLIP_QUESTION, content, "heard", SOUNDS)
    found.heard = tags
    return {"heard": list(tags)}, {"heard": list(tags)}, paid


def _look_photo(
    ember: EmberClient, scene: Scene, found: Found, _args: Mapping[str, Any]
) -> _Result:
    if found.seen is not None:
        kept = {"seen": list(found.seen), "again": True}
        return kept, kept, None
    photo = scene.media(scene.tap_id, "photo")
    if photo is None:
        return {"error": "no_photo"}, {"error": "no_photo"}, None
    content = {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64," + base64.b64encode(photo).decode(),
    }
    tags, paid = _media_tags(ember, "look_photo", _PHOTO_QUESTION, content, "seen", SIGHTS)
    found.seen = tags
    return {"seen": list(tags)}, {"seen": list(tags)}, paid


def _media_tags(
    ember: EmberClient,
    task: str,
    question: str,
    content: Mapping[str, Any],
    name: str,
    allowed: Sequence[str],
) -> tuple[tuple[str, ...], Accounting]:
    """Have Ember say which of `allowed` a tap's clip or photo holds. Only tags
    from the list are read from its answer; an answer that is no form reads as
    `none`, which is what is known of a clip nothing was made of."""
    answer = ember.respond(
        task=task,
        effort="low",
        max_output_tokens=MEDIA_OUTPUT_TOKENS,
        # A clip can hold a voice and a photo a face.
        sensitive=True,
        input=[{"role": "user", "content": [{"type": "input_text", "text": question}, content]}],
        text={
            "format": {
                "type": "json_schema",
                "name": f"tap_{name}",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        name: {"type": "array", "items": {"type": "string", "enum": list(allowed)}}
                    },
                    "required": [name],
                    "additionalProperties": False,
                },
            }
        },
    )
    try:
        said = json.loads(answer.output_text)
    except ValueError:
        said = None
    given = said.get(name) if isinstance(said, dict) else None
    tags = [tag for tag in allowed if isinstance(given, list) and tag in given and tag != "none"]
    return tuple(tags) or ("none",), Accounting.of(answer)


_TOOLS: dict[str, Callable[[EmberClient, Scene, Found, Mapping[str, Any]], _Result]] = {
    "nearby_incidents": _nearby_incidents,
    "get_radio": _get_radio,
    "get_aircraft": _get_aircraft,
    "get_official": _get_official,
    "nearby_taps": _nearby_taps,
    "listen_clip": _listen_clip,
    "look_photo": _look_photo,
}


def _tag(value: object, allowed: Sequence[str]) -> str | None:
    """A field that should hold one of `allowed`, when it does. Else None: what a
    row holds in a tag's place is not handed on because it is text."""
    return value if isinstance(value, str) and value in allowed else None


def _alarm(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5
        else None
    )


def _rounded(metres: float) -> int:
    return int(round(metres / _ROUND_M) * _ROUND_M)


def _minutes(span: timedelta) -> int:
    return max(0, round(span.total_seconds() / 60))
