"""What an incident is, as the engine hands it on.

An incident is derived: it is worked out from the evidence store's rows each time, and
nothing here is stored. Every line of it that says something (`Entry`, `Official`,
`Signal`) names the rows it rests on, so a claim always carries its source and how
far that source is to be trusted.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from beacon.evidence import Confidence, Kind, Point
from beacon.incident.aircraft import Track

# Confidence, strongest first: the order sources are ranked in when they disagree.
CONFIDENCE_RANK: tuple[Confidence, ...] = ("official", "high", "medium", "low")

EntryKind = Literal["radio", "medical", "closure", "alert", "notice", "aircraft", "taps"]
SignalKind = Literal[
    "alarm",
    "evacuation_on_radio",
    # A notice that tells people to do something was published: an order or a
    # warning. The kind says no more than that; the signal's text names each type,
    # and `Incident.official` lists them.
    "official_notice",
    "official_alert",
    "closure",
    "many_units",
    "aircraft_circling",
    "taps",
]
ConflictKind = Literal[
    # An all-clear was reported on the radio after an official alert for the place was
    # issued that is in force, or after an order or a warning was published.
    "all_clear_on_radio",
    # A notice that names a street of the incident, published after the radio's
    # calls said what it is, is about another hazard.
    "hazard",
]
OfficialState = Literal[
    "in_force", "replaced", "cancelled", "not_listed", "ended", "unknown", "published"
]


@dataclass(frozen=True)
class CreditPart:
    """A piece of a credit's words, and the address it links to when it is a link."""

    text: str
    url: str | None = None


@dataclass(frozen=True)
class Credit:
    """Words a source's terms require beside anything shown from it, and their link."""

    text: str
    url: str
    # The same words in pieces, for a credit whose terms ask for more than one link:
    # the name of a database to one address, and of its license to another. Empty for
    # a credit that is one link, which is `text` to `url`.
    parts: tuple[CreditPart, ...] = ()


@dataclass(frozen=True)
class Source:
    """Where a claim comes from: one evidence row, or the taps counted together."""

    kind: Kind
    # The feed, as the evidence store names it: `openmhz`, `nws`, `511`, `notices`,
    # `adsblol`, `app`.
    source: str
    # The feed in words, for a person.
    label: str
    confidence: Confidence
    at: datetime
    # The row. None for taps, which are counted and never pointed at one by one: a
    # tap's id is what its sender reads the answer with.
    evidence_id: int | None
    # Where a person can read the original, when it is public.
    url: str | None
    # Whether a stand-in had a part in the row: the invented scenario, or a fake Ember.
    stand_in: bool
    # Which kind of Ember client wrote the row's data, when a model did.
    model: str | None = None
    credit: Credit | None = None


@dataclass(frozen=True)
class Entry:
    """One line of an incident's timeline: something one source said."""

    at: datetime
    kind: EntryKind
    # The line in words, written by code from the row's own fields.
    text: str
    # How far to trust the line, which is how far its source is trusted.
    confidence: Confidence
    sources: tuple[Source, ...]
    # The fields the line was written from, for an app that draws it its own way.
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Official:
    """An alert or a notice an authority issued about the incident's place, and how
    it stands. Beacon relays these and adds no instruction of its own."""

    kind: Literal["alert", "notice"]
    # An alert's event, as the authority named it (`Shelter In Place Warning`), or a
    # notice's type (`evacuation_order`).
    type: str
    # `published` is a notice: a document that was published, and of which Beacon
    # says no more. It does not say that an order is in force, and it does not say
    # that an all-clear lifts one: a notice is `ended` only when it ran to the time
    # its own document gave. `unknown` is an alert that may have been ended by a row
    # that was not read.
    state: OfficialState
    # The row that ended an alert, when one did: an update, a cancellation, or the
    # row that says it is no longer listed. None for a notice.
    ended_by: int | None
    issued_at: datetime
    until: datetime | None
    issuer: str | None
    # For an alert, the authority's own words. For a notice, a model's reading of
    # the document, and only what the redaction pass let through whole
    # (`beacon.redact.reading`): show it as Beacon's reading, beside `source.url`,
    # and never as the authority's words or as what to do.
    headline: str | None
    instruction: str | None
    # True for a notice: what Beacon has is a model's reading of the document at
    # `source.url`, which is the authority speaking.
    reading: bool
    source: Source
    # The fields that are not all here because there was more than Beacon keeps:
    # an alert's text that may have been cut, and what a notice's row says of its
    # own (`data["incomplete"]`). Whoever shows it says it has more, and links the
    # original.
    incomplete: tuple[str, ...] = ()
    # A notice's summary and the areas it names, as a model read them, on the same
    # terms as `instruction`. The areas are all there or none is.
    summary: str | None = None
    areas: tuple[str, ...] = ()
    # The fields of a notice that the row holds and the redaction pass did not let
    # through, so they are left out whole: `issuer`, `headline`, `instruction`,
    # `summary`, `areas`. Whoever shows the notice says that it has more.
    withheld: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conflict:
    """Radio traffic and an official source that do not say the same thing about an
    incident. The official source outranks the radio, and `text` says so and why.
    Code finds these and words them; no model weighs one source against another."""

    kind: ConflictKind
    text: str
    # How far to trust that the two disagree, which is as far as the weakest row
    # it rests on: a call that was only partly made out makes it `low`.
    confidence: Confidence
    # The official rows, which are the ones that hold, and the radio rows.
    official: tuple[Source, ...]
    radio: tuple[Source, ...]


@dataclass(frozen=True)
class Signal:
    """Something about an incident that can make it more than an everyday call. The
    signals are worked out by code. Whether they add up to a crisis is not decided
    here."""

    kind: SignalKind
    text: str
    # How far to trust it, which is as far as the weakest row it rests on.
    confidence: Confidence
    # The rows it rests on, each with its feed, its link, and the credit its
    # source's terms require, so a signal shown by itself still carries them.
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Incident:
    # Names the incident for as long as the row that opened it is its first row.
    id: str
    # `medical response` for a medical call, else the category of its calls.
    category: str
    title: str
    status: str
    # Whether it is still going: its radio has not cleared it and has been heard
    # from lately, or an official alert about it is in force.
    open: bool
    opened_at: datetime
    updated_at: datetime
    # No finer than a block.
    place: str | None
    point: Point | None
    # Where the point is from: the list of blocks, or a closure 511 lists on its street.
    point_from: Literal["blocks", "closure"] | None
    # The row a point from a closure was taken from, with 511's credit, which goes
    # wherever the point is shown. None for a point from the list of blocks.
    point_source: Source | None
    streets: tuple[str, ...]
    units: tuple[str, ...]
    alarm: int | None
    timeline: tuple[Entry, ...]
    official: tuple[Official, ...]
    aircraft: tuple[Track, ...]
    # How many taps came from near it while it was going on. Taps, not people:
    # nothing tells one sender from another.
    taps: int
    # Whether there were more taps than were read, so that `taps` is a lower bound,
    # and whether there were more positions of aircraft, so that a track can be
    # missing or cut short.
    taps_more: bool
    aircraft_more: bool
    signals: tuple[Signal, ...]
    # Where the radio and an official source disagree, and which holds.
    conflicts: tuple[Conflict, ...]
    # Whether any row it is built from is a stand-in's. It is then one too.
    stand_in: bool
    # The rows it is built from, taps left out.
    evidence_ids: tuple[int, ...]
