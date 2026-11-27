"""One scale run: a day of calls heard, in a directory of its own.

A run lives under `data/scale/<name>/` (`Layout`), which Git ignores:

    run.json          what the run is: its day, what its audio is, and who hears it
    ledger.json       which Ember ledger a paid run began to pay in (`pin_ledger`)
    records.json      DataSF's calls of the day, as Beacon keeps them (`datasf.Record`)
    lists/            streets.txt, units.txt, blocks.csv (`beacon.scale.lists`)
    calls/            generated calls, marked BEACON-GENERATED (`beacon.scale.generate`)
    evidence.sqlite3  the run's own evidence store: never the server's
    heard.json        that the hearing is closed, from when it was first complete
    taps.sqlite3      the investigations of the replayed taps (`beacon.scale.taps`),
                      and the run's hold on Ember
    unit_types.2.json DataSF's unit types for each unit of the day, which the glossary
                      is compared with (`beacon.scale.glossary`)
    glossary.json     the claim on the run's glossary, and the glossary
    glossary_hold.json  what the glossary's call did to the run's hold, until
                      taps.sqlite3 has it (`glossary.settle`)
    results.json      what the run came to (`beacon.scale.results`)

**What a run is is decided once and kept** (`run.json`): generated tones heard by a
stand-in, generated speech heard by a stand-in or by Muse Ember, or recordings of
real radio heard by Muse Ember. A run that was begun as one is not gone on with as
another: its rows would then be of two kinds that nothing tells apart afterwards,
and a number could be read as the wrong one's. Start another run under another name.

**A call is paid for once, over every invocation.** Hearing is the recorded-radio
poller's with a backlog (`beacon.ingest.recordings`): a call is claimed in the run's
evidence store before Ember is asked, so a run that is stopped (by its operator, a
spending stop, or a failure) goes on, when the same command is given again, with the
calls nobody claimed. A paid run pays under a scope of its kind of audio,
`scale:radio` for recordings and `scale:speech` for generated speech, so that the
cost the ledger holds under one is never a mix of the two. A scope is counted in the
Ember ledger over every invocation and every run directory, and the two together are
held to `RADIO_BUDGET_NANOUSD`, which a setting can lower and not raise: a run's
scope gets that budget less what the other scope holds when the run starts
(`radio_ember`). That reading is safe because one invocation is made at a time
under a data directory (`alone`), and because a paid run goes on only in the ledger
it began to pay in (`pin_ledger`): the taps' client is made first and the ledger
pinned with it, the other scope's spend is read from that client's file, and the
hearing client has to have been made on the same file. Both are made before
anything is heard, so a ledger that is replaced part of the way stops the taps as
it stops the hearing, and the run is then not gone on with. A run whose hearing is
closed makes no hearing client and needs nothing left of the hearing's budget: it
can only pay for taps and for its glossary, each under a scope of its own.

**The hearing is closed when it is first complete** (`close_hearing`), before the
taps are replayed and the glossary is learned, so that everything made of the whole
day is of the same evidence.

**Every call, a stand-in's too, is booked in the one Ember ledger** under the data
directory, as the server's are: a run has no ledger of its own. A stand-in's rows
there say `fake` and cost nothing.

**After Ember refuses a call or fails on one, the run leaves it alone** for ten
minutes, counted from the failure and kept in the run's evidence store, as the
server's poller does. The same command given sooner stops at once and says so.
"""

import contextlib
import fcntl
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from beacon.ember import (
    BudgetExceededError,
    EmberClient,
    EmberError,
    LedgerReplacedError,
    SpendScope,
    Storage,
)
from beacon.ember.client import default_data_dir
from beacon.ember.ledger import ScopeSpend
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.incident.places import Blocks, read_blocks
from beacon.ingest.http import SourceError, SourceHttp
from beacon.ingest.paid import read_hold
from beacon.ingest.recordings import MAX_LISTED, Directory, PassCount, RecordedRadio
from beacon.ingest.recordings import SOURCE as RADIO_SOURCE
from beacon.ingest.runner import PollerHeldError
from beacon.radio import Ffmpeg, Known, core_dump_problem, forbid_core_dumps
from beacon.radio.vocabulary import read_names
from beacon.scale import datasf, generate, lists

# The spending scopes of a scale run. Hearing has one for each kind of audio, so
# that what the ledger holds under one is the cost of that kind and no other:
# recorded radio, and generated speech. The two together may hold
# `RADIO_BUDGET_NANOUSD` in the ledger over every run, and the replayed taps
# `TAPS_BUDGET_NANOUSD`: the build plan's budget for the full-day run and for the
# replayed taps.
RADIO_SCOPE = "scale:radio"
SPEECH_SCOPE = "scale:speech"
TAPS_SCOPE = "scale:taps"
RADIO_BUDGET_NANOUSD = 1_250_000_000
TAPS_BUDGET_NANOUSD = 500_000_000

Audio = Literal["generated", "recorded"]
Hearer = Literal["fake", "live"]
# Why a hearing stopped before a pass over the calls ended, from a closed list.
Stopped = Literal["budget", "held", "ledger_replaced", "ember", "source", "store"]

_CALLS_PER_POLL = 200
# How long after a day's end its recorded calls are still heard.
AFTER_DAY = timedelta(hours=2)


class ScaleError(Exception):
    """A scale run cannot be made as it was asked for. Its text is fixed sentences
    and settings' names: nothing of a call or of a source's reply."""


@dataclass(frozen=True)
class Layout:
    """Where a run's files are."""

    root: Path

    @classmethod
    def under(cls, env: Mapping[str, str], name: str) -> "Layout":
        """The run of that name under the data directory.

        Raises ScaleError when the name is not a plain one."""
        plain = name.replace("-", "").replace("_", "")
        if not plain.isalnum() or not plain.isascii() or len(name) > 40:
            raise ScaleError("A run is named with letters, digits, - and _, up to 40 of them.")
        return cls(default_data_dir(env) / "scale" / name)

    @property
    def settings(self) -> Path:
        return self.root / "run.json"

    @property
    def ledger(self) -> Path:
        return self.root / "ledger.json"

    @property
    def closed(self) -> Path:
        return self.root / "heard.json"

    @property
    def records(self) -> Path:
        return self.root / "records.json"

    @property
    def lists(self) -> Path:
        return self.root / "lists"

    @property
    def calls(self) -> Path:
        return self.root / "calls"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence.sqlite3"

    @property
    def taps(self) -> Path:
        return self.root / "taps.sqlite3"

    @property
    def results(self) -> Path:
        return self.root / "results.json"


@dataclass(frozen=True)
class Settings:
    """What a run is, for as long as its directory is there."""

    # The day whose calls it is of, San Francisco's.
    day: str
    audio: Audio
    # What generated audio is: tones, or synthesized speech. None for recordings.
    voice: generate.Voice | None
    # Who hears the calls: a stand-in for Ember, or Muse Ember, paid.
    hearer: Hearer
    # For recordings: the recorder's system and the talkgroups that are heard. The
    # directory itself is named at each invocation and is not kept.
    # Generated calls have two talkgroups, and the medical calls' is heard as an
    # EMS-only one.
    system: str = generate.SYSTEM
    talkgroups: tuple[int, ...] = (generate.TALKGROUP, generate.EMS_TALKGROUP)
    ems_talkgroups: tuple[int, ...] = (generate.EMS_TALKGROUP,)
    # For generated calls: how many calls a sample holds. Only the first that many
    # of the day's calls, in the order they were dispatched, are generated, heard,
    # and scored. None for a run of the whole day. On disk a sampled run's audio
    # is `SAMPLED_AUDIO` (`begin`, `read_settings`).
    sample: int | None = None

    @property
    def mode(self) -> Literal["mock", "live"]:
        """The mode of the run's evidence store: generated audio's rows are a
        stand-in's whoever hears them, and only recordings' are live."""
        return "live" if self.audio == "recorded" else "mock"


@dataclass(frozen=True)
class Heard:
    """What one invocation's hearing came to."""

    # The last pass over the calls that ended, or None when none did.
    last_pass: PassCount | None
    # Why the hearing stopped before a pass ended, or None when one ended.
    stopped: Stopped | None
    # What the Ember ledger held under the run's hearing scope when the hearing
    # ended, over every run paid under it. None when a stand-in heard the calls,
    # and when the ledger is not the one the run began to pay in.
    spend: ScopeSpend | None = None
    # The Ember ledger was replaced under the run: what was spent is not known.
    ledger_lost: bool = False


@contextlib.contextmanager
def alone(env: Mapping[str, str]) -> Iterator[None]:
    """Hold the one lock of the scale runs under a data directory, for as long as
    an invocation lasts. One invocation is made at a time, a stand-in's too: two
    paid ones would each read what the other's scope holds before either had spent,
    and two of one name would write one run's files. The lock is the operating
    system's, on an open file, so it is let go of when the process ends, however
    it ends.

    Raises ScaleError when another invocation holds it."""
    directory = default_data_dir(env) / "scale"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a", encoding="utf-8") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScaleError(
                "Another scale run is under way under this data directory. One is made "
                "at a time: wait for it to end, then give the command again."
            ) from None
        yield


LEDGER_REPLACED = (
    "The Ember ledger is not the one this run began to pay in: it was replaced, so "
    "what the run spent is not known from it. Compare Meta's Usage page with the "
    "ledger before any more paid work. This run is not gone on with."
)


def pin_ledger(layout: Layout, ember: EmberClient) -> None:
    """Keep which ledger file a paid run began to pay in when the run is new, and
    refuse to go on in another. A run's caps are counted in the ledger: in one that
    was begun again, or put back from an older copy, they would be counted from
    less than was spent. The file is told by its generation, its device, and its
    inode (`EmberClient.ledger_file`), so a copy put back by renaming is another
    file. **What this cannot tell** is a ledger rolled back in place, as a copy
    written over the same file is: the file is the same one and holds less.

    Raises ScaleError when the ledger is another, or was seen to be replaced."""
    kept = _pinned(layout)
    if kept is None:
        _write(layout.ledger, {"file": ember.ledger_file(), "lost": False})
    elif kept != ember.ledger_file():
        raise ScaleError(LEDGER_REPLACED)


def lose_ledger(layout: Layout) -> None:
    """Record that the ledger was replaced under a paid run, so that the run is
    not gone on with."""
    _write(layout.ledger, {"file": None, "lost": True})


def _pinned(layout: Layout) -> str | None:
    """The name of the ledger file a run pays in, or None for a run that has not
    paid. Raises ScaleError for a run whose ledger was lost."""
    try:
        said = json.loads(layout.ledger.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        said = None
    if not isinstance(said, dict) or said.get("lost") or not isinstance(said.get("file"), str):
        raise ScaleError(LEDGER_REPLACED)
    return said["file"]


def scope_spend(ember: EmberClient, label: str) -> tuple[ScopeSpend | None, bool]:
    """What the ledger a paying client was made on holds under a scope, and whether
    that ledger was replaced, in which case nothing is known of the spend: what the
    file at its path holds now is another file's."""
    if ember.ledger_replaced():
        return None, True
    spend = ember.scope_spend(label)
    # Once more, after the read: the file can have been replaced between the two.
    return (None, True) if ember.ledger_replaced() else (spend, False)


def hearing_spend(settings: Settings, ember: EmberClient) -> tuple[ScopeSpend | None, bool]:
    """What the ledger a paying client was made on holds under the run's hearing
    scope, and whether that ledger was replaced (`scope_spend`). The client is any
    of the run's that pays: the taps' reads it for a run whose hearing is closed."""
    return scope_spend(ember, hearing_scope(settings))


def held_for(
    store: EvidenceStore, wall_clock: Callable[[], datetime] | None = None
) -> float | None:
    """How many seconds the hearing's hold still has to run, or None when none is
    on: the hold the poller put on after a refusal or a failure and kept in the
    run's evidence store, which is ten minutes or longer."""
    held_until, _ = read_hold(store.state(RADIO_SOURCE))
    if held_until is None:
        return None
    now = datetime.now(UTC) if wall_clock is None else wall_clock()
    left = (held_until - now).total_seconds()
    return left if left > 0 else None


def close_hearing(layout: Layout, last_pass: PassCount) -> None:
    """Close a run's hearing for good, which is done when it is first complete,
    before anything is made of the whole day: the replay of its taps and its
    glossary. Every tap is investigated against the run's evidence and the
    glossary is learned from its incidents, each once, and what they came to is
    kept for good: were calls heard afterwards, a result would put the old answers
    and the old glossary beside the new incidents. So from then on nothing more
    is heard in this run; calls that turn up later want a new run."""
    _write(layout.closed, asdict(last_pass))


def closed_hearing(layout: Layout) -> PassCount | None:
    """The last pass of a run whose hearing was closed, or None while it is open.

    Raises ScaleError when the record of it cannot be read."""
    try:
        said = json.loads(layout.closed.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        said = None
    try:
        counts = {key: said[key] for key in ("stored", "unready", "unusable")}
        if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts.values()):
            return PassCount(**counts)
    except (KeyError, TypeError):
        pass
    raise ScaleError("The run's record of its closed hearing (heard.json) cannot be read.")


def day_window(day: date) -> tuple[datetime, datetime]:
    """The time a day's recorded calls are in: from the start of San Francisco's
    day to `AFTER_DAY` after its end, in which the day's last calls are cleared.
    The day is the city's civil one, which is 23 or 25 hours long twice a year."""
    following = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=datasf.LOCAL)
    end = datetime(following.year, following.month, following.day, tzinfo=datasf.LOCAL)
    return start.astimezone(UTC), end.astimezone(UTC) + AFTER_DAY


# What a sampled run's settings say its audio is, on disk only. A Beacon from
# before a run could be of a sample knows no such audio and refuses the run; it
# would otherwise read past the `sample` it does not know, take the sample's
# records for the day's, and go on with the run, and pay, as a whole day's.
SAMPLED_AUDIO = "generated-sample"


def begin(layout: Layout, asked: Settings) -> Settings:
    """Keep what a run is when it is new, and say what it is.

    Raises ScaleError when the directory holds a run that was begun as another."""
    kept = read_settings(layout)
    if kept is None:
        layout.root.mkdir(parents=True, exist_ok=True)
        said = asdict(asked)
        if asked.sample is not None:
            said["audio"] = SAMPLED_AUDIO
        _write(layout.settings, said)
        return asked
    if kept != asked:
        raise ScaleError(
            "This run was begun as another: its day, its audio, who hears it, its "
            "talkgroups, or its sample are not the ones asked for now. A run is one kind for good, "
            "so that its numbers are one kind's. Give the new run another --name."
        )
    return kept


def read_settings(layout: Layout) -> Settings | None:
    """What the run in a directory is, or None when there is none.

    Raises ScaleError when its settings cannot be read."""
    try:
        said = json.loads(layout.settings.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        said = None
    try:
        # A sample is said twice, by the audio and by the count, and a run that
        # says one without the other is none this Beacon made.
        sampled = said["audio"] == SAMPLED_AUDIO
        if sampled != (said.get("sample") is not None):
            raise ValueError
        settings = Settings(
            day=date.fromisoformat(said["day"]).isoformat(),
            audio="generated" if sampled else said["audio"],
            voice=said["voice"],
            hearer=said["hearer"],
            system=str(said["system"]),
            talkgroups=tuple(int(number) for number in said["talkgroups"]),
            ems_talkgroups=tuple(int(number) for number in said["ems_talkgroups"]),
            # A run begun before a run could be of a sample says nothing of one,
            # and is of its whole day.
            sample=said.get("sample"),
        )
        if (
            not _is_sample(settings)
            or settings.audio not in ("generated", "recorded")
            or settings.hearer not in ("fake", "live")
            or settings.voice not in (None, "tones", "speech")
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ScaleError("The run's settings (run.json) cannot be read.") from None
    return settings


def _is_sample(settings: Settings) -> bool:
    """Whether a run's sample is one a run can have: none, or a count of at least
    one for generated calls. Recordings are heard as they were made."""
    sample = settings.sample
    if sample is None:
        return True
    whole = isinstance(sample, int) and not isinstance(sample, bool) and sample >= 1
    return whole and settings.audio == "generated"


def prepare(layout: Layout, settings: Settings, http: SourceHttp) -> list[datasf.Record]:
    """Fetch what a run is scored against and reads names from, once: DataSF's
    calls of the day, and the lists of streets, blocks, and units. For generated
    audio, write the calls. What is already there is read and not fetched again.

    A run of a sample keeps only the sample's records, the first of the day in the
    order they were dispatched, with how many the day had: everything that is made
    of the run's records afterwards (the generated calls, the score, the taps) is
    then of the sample and of nothing else.

    Raises SourceError when DataSF cannot be read, and ScaleError when the day has
    no calls, has no more calls than the sample asks for, the lists come out
    empty, or the calls cannot be written."""
    day = date.fromisoformat(settings.day)
    if not layout.records.exists():
        records, unread = datasf.read_records(datasf.fetch_day(http, day))
        if not records:
            raise ScaleError("DataSF lists no call for that day that Beacon can read.")
        day_calls = len(records)
        if settings.sample is not None:
            if settings.sample >= day_calls:
                raise ScaleError(
                    "A sample holds fewer calls than the day has: DataSF's record of "
                    f"that day has {day_calls} that Beacon can read. Make the run "
                    "without --calls, under another --name."
                )
            # `read_records` gives them oldest first by when each was dispatched.
            records = records[: settings.sample]
        _write(
            layout.records,
            {
                "unreadable_calls": unread.calls,
                "unreadable_rows": unread.rows,
                "day_calls": day_calls,
                "records": [record.as_data() for record in records],
            },
        )
    records = read_records(layout)
    # Before anything is generated, heard, or paid for, at every invocation: the
    # records kept are what the run's settings say they are.
    read_day_calls(layout, settings)
    if not (layout.lists / lists.BLOCKS_FILE).exists():
        made = lists.lists_of(lists.fetch_centerlines(http), lists.fetch_roster(http, day))
        if not (made.streets and made.units and made.blocks):
            raise ScaleError("DataSF's streets or units came out as an empty list.")
        made.write(layout.lists)
    if settings.audio == "generated" and not (layout.calls / generate.SCRIPT_FILE).exists():
        script, unplaced = generate.script_of(records, read_lists(layout)[1])
        try:
            assert settings.voice is not None
            generate.write_calls(layout.calls, script, settings.voice, unplaced=unplaced)
        except generate.GenerateError as error:
            raise ScaleError(str(error)) from None
    return records


def read_records(layout: Layout) -> list[datasf.Record]:
    """The records a run kept. Raises ScaleError when they cannot be read."""
    return read_day(layout)[0]


def read_day(layout: Layout) -> tuple[list[datasf.Record], datasf.Unread]:
    """The records a run kept, and what of DataSF's answer could not be read as a
    call and so is in no count. Raises ScaleError when they cannot be read."""
    try:
        said = json.loads(layout.records.read_text(encoding="utf-8"))
        counts = (said["unreadable_calls"], said["unreadable_rows"])
        if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts):
            raise ValueError
        return datasf.records_from_data(said["records"]), datasf.Unread(*counts)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise ScaleError("The run's records of DataSF's calls (records.json) cannot be read.")


def read_day_calls(layout: Layout, settings: Settings) -> int:
    """How many calls of the day Beacon could read in DataSF's record. A run of the
    whole day kept them all, and one begun before the number was kept says none:
    for both it is how many were kept. A run of a sample kept the first of them,
    as many as its settings say and fewer than the day has, and says how many the
    day has: one that does not is not gone on with.

    Raises ScaleError when the run's records cannot be read, or are not what its
    settings say they are."""
    kept = len(read_day(layout)[0])
    try:
        said = json.loads(layout.records.read_text(encoding="utf-8")).get("day_calls")
    except (OSError, ValueError, AttributeError):
        said = False
    counted = isinstance(said, int) and not isinstance(said, bool)
    if settings.sample is None:
        if said is None or (counted and said == kept):
            return kept
    elif counted and kept == settings.sample < said:
        return said
    raise ScaleError(
        "The run's records of DataSF's calls (records.json) are not what the run's "
        "settings say they are: all of the day's calls, or the first of them that "
        "its sample holds."
    )


def read_lists(layout: Layout) -> tuple[Known, Blocks]:
    """The streets and units a row of the run may name, and where its blocks are.

    Raises ScaleError when the run's lists cannot be read."""
    try:
        streets = read_names((layout.lists / lists.STREETS_FILE).read_text(encoding="utf-8"))
        units = read_names((layout.lists / lists.UNITS_FILE).read_text(encoding="utf-8"))
        blocks, _ = read_blocks((layout.lists / lists.BLOCKS_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise ScaleError("The run's lists of streets, units, and blocks cannot be read.") from None
    known, _ = Known.kept(streets=streets, units=units)
    return known, blocks


def open_store(layout: Layout, settings: Settings) -> EvidenceStore:
    """The run's own evidence store."""
    return EvidenceStore(layout.evidence, settings.mode)


# How a paid client is made for a scope: `EmberClient.from_env`, unless a test hands
# in a live client over a transport of its own.
PaidClient = Callable[[SpendScope], EmberClient]


def radio_ember(
    layout: Layout,
    settings: Settings,
    env: Mapping[str, str],
    budget_nanousd: int,
    make: PaidClient | None = None,
    *,
    beside: EmberClient | None = None,
) -> EmberClient:
    """The client that hears a run's calls: a stand-in that reads the generated
    calls' script, or Muse Ember under the scope of the run's kind of audio
    (`hearing_scope`), whose budget is `budget_nanousd` less what the other kind's
    scope holds, so that hearing is held to one budget over both.

    A paying client is made `beside` the paying client the run's ledger was pinned
    with (`pin_ledger`), and what the other scope holds is read from that client's
    ledger file and no other: the file is seen to be the same one before the read
    and after it, and the new client has to have been made on that same file. A
    ledger put in its place between the read and the making, which could hold more
    under the other scope than was read, is refused here, before anything is heard.

    Raises ScaleError when the budget is more than the plan's or none of it is
    left, when a paid run is asked for without a live Ember, when the ledger is not
    the one that was pinned, and when the script cannot be read. Raises ValueError
    for a paid run with no client to be made beside."""
    if settings.hearer == "fake":
        try:
            script = generate.read_script(layout.calls)
        except generate.GenerateError as error:
            raise ScaleError(str(error)) from None
        return EmberClient.fake(Storage.under(default_data_dir(env)), script.hearer)
    if beside is None or beside.mode != "live":
        raise ValueError("A paying hearer is made beside the client the ledger was pinned with.")
    label = hearing_scope(settings)
    check_budget(label, budget_nanousd, RADIO_BUDGET_NANOUSD)
    other = SPEECH_SCOPE if label == RADIO_SCOPE else RADIO_SCOPE
    held, lost = scope_spend(beside, other)
    if lost or held is None:
        raise ScaleError(LEDGER_REPLACED)
    left = budget_nanousd - held.spent_nanousd
    if left <= 0:
        raise ScaleError(
            f"Nothing is left of the budget for hearing: the scope {other} holds it "
            "all, and the two kinds of paid run share one budget."
        )
    # Refused when it was made on another file than the one the other scope's
    # spend was read from.
    return paid_ember(env, label, left, RADIO_BUDGET_NANOUSD, make, beside=beside)


def hearing_scope(settings: Settings) -> str:
    """The scope a paid run's hearing is paid under: its kind of audio's."""
    return RADIO_SCOPE if settings.audio == "recorded" else SPEECH_SCOPE


def check_budget(label: str, budget_nanousd: int, most: int) -> None:
    """Raises ScaleError for a budget that is nothing, or more than the plan's."""
    if not 0 < budget_nanousd <= most:
        raise ScaleError(
            f"The budget of {label} is more than nothing and at most ${most / 1e9:.2f}, "
            "which is the build plan's and is not raised by a setting."
        )


def paid_ember(
    env: Mapping[str, str],
    label: str,
    budget_nanousd: int,
    most: int,
    make: PaidClient | None = None,
    *,
    beside: EmberClient | None = None,
) -> EmberClient:
    """A live Ember client that pays under the scope `label`, whose budget is at
    most `most`. With `beside`, the paying client the run's ledger was pinned
    with, the new client has to have been made on that client's ledger file: a
    scope's cap is counted in the ledger, and in a file put in its place the scope
    could hold less than was spent.

    Raises ScaleError when the budget is not one, there is no live Ember, or the
    ledger is not the one `beside` is on."""
    check_budget(label, budget_nanousd, most)
    scope = SpendScope(label, budget_nanousd)
    ember = EmberClient.from_env(env, scope=scope) if make is None else make(scope)
    if ember.mode != "live":
        ember.close()
        raise ScaleError(
            "A paid run needs a live Ember: set REPLAY=0, MODEL_API_KEY, and "
            "EMBER_SPENT_BEFORE_USD. Without --paid a stand-in hears generated calls."
        )
    if beside is not None and (
        ember.ledger_file() != beside.ledger_file() or beside.ledger_replaced()
    ):
        ember.close()
        raise ScaleError(LEDGER_REPLACED)
    return ember


def hear(
    layout: Layout,
    settings: Settings,
    store: EvidenceStore,
    ember: EmberClient,
    env: Mapping[str, str],
    *,
    recordings: Path | None = None,
    now: datetime | None = None,
    wall_clock: Callable[[], datetime] | None = None,
) -> Heard:
    """Have every call of the run that nobody has claimed heard, until a pass over
    the calls has ended or something stops it. `ember` is closed when it returns.
    Of a directory of recordings, only the calls of the run's day are heard: the
    ones whose files' names say they began on San Francisco's day, or in the two
    hours after its end, in which the day's last calls are cleared (`day_window`).
    `wall_clock` is the time a hold after a failure is counted by: the real time,
    unless a test moves it.

    Raises ScaleError when the calls' directory is not what the run is of, and
    when the day's last calls can still be recorded."""
    now = datetime.now(UTC) if now is None else now
    since = None
    transcoder = None
    try:
        if settings.audio == "recorded":
            since, until = day_window(date.fromisoformat(settings.day))
            if now < until:
                # A pass that ended now would be taken for the whole day's.
                raise ScaleError(
                    "The day's last calls can still be recorded: a run over recordings "
                    "is made from two hours after the end of San Francisco's day."
                )
            now = until
        root = layout.calls if settings.audio == "generated" else recordings
        if root is None:
            raise ScaleError("A run over recordings needs their directory: --recordings.")
        try:
            shelf = Directory(root, settings.system)
        except ValueError:
            raise ScaleError(
                "The directory of calls cannot be opened as one system's recorded "
                "calls, or it cannot be told whether it is marked BEACON-GENERATED."
            ) from None
        if shelf.generated != (settings.audio == "generated"):
            raise ScaleError(
                "The directory of calls is not what this run is of: recordings carry "
                "no BEACON-GENERATED mark, and generated calls carry it."
            )
        if settings.audio == "recorded":
            # This process will hold real radio audio in memory, as the server's
            # poller does (`beacon.ingest.setup`): no core file, and a confined
            # decoder for a call that is not WAV.
            transcoder = Ffmpeg(hidden=[default_data_dir(env)])
            problem = transcoder.problem() or core_dump_problem()
            if problem is not None:
                raise ScaleError(problem)
            forbid_core_dumps()
        known, _ = read_lists(layout)
        try:
            poller = RecordedRadio(
                shelf,
                ember,
                talkgroups=settings.talkgroups,
                ems_talkgroups=settings.ems_talkgroups,
                known=known,
                backlog=True,
                since=since,
                calls_per_poll=min(_CALLS_PER_POLL, MAX_LISTED),
                scope=hearing_scope(settings),
                transcoder=transcoder,
                wall_clock=wall_clock,
            )
        except ValueError as error:
            # The poller's own fixed sentence: who may hear what, and under which scope.
            raise ScaleError(str(error)) from None
        heard = _passes(poller, store, now)
        if ember.mode == "live":
            spend, lost = hearing_spend(settings, ember)
            if heard.stopped == "ledger_replaced" or lost:
                # What the file at the ledger's path holds now is not what was spent.
                heard = replace(heard, ledger_lost=True)
            else:
                heard = replace(heard, spend=spend)
        return heard
    finally:
        ember.close()
        if transcoder is not None:
            transcoder.close()


def _passes(poller: RecordedRadio, store: EvidenceStore, now: datetime) -> Heard:
    """Poll until a pass over the calls has ended, or a poll fails."""
    stopped: Stopped | None = None
    try:
        while poller.poll(store, now).partial:
            pass
    except PollerHeldError:
        stopped = "held"
    except LedgerReplacedError:
        stopped = "ledger_replaced"
    except BudgetExceededError:
        stopped = "budget"
    except EmberError:
        stopped = "ember"
    except SourceError:
        stopped = "source"
    except EvidenceStoreError:
        stopped = "store"
    return Heard(last_pass=None if stopped is not None else poller.last_pass, stopped=stopped)


def _write(path: Path, said: Any) -> None:
    """Write a JSON file whole: to a file beside it, which then takes its place."""
    beside = path.with_name(path.name + ".part")
    beside.write_text(json.dumps(said, indent=1), encoding="utf-8")
    os.replace(beside, path)
