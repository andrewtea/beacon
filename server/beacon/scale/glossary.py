"""The scale run's glossary: one comparison of what Beacon knows of unit type codes
before and after `learn_glossary`, on the run's own day.

**What is learned from.** The run's incidents, as the incident engine made them of
the run's radio rows: for each unit type code, counts by category and by company
(`patterns_of`). No transcript is involved, because Beacon keeps none.

**What it is compared with.** DataSF publishes, beside each unit sent to a call, the
unit's type (`ENGINE`, `MEDIC`, `RESCUE CAPTAIN`). `fetch_unit_types` asks for the
ids and types of the units of the run's day, and `truth_of` reads from them what
kind of unit each code names, where the units of a code are mostly of one kind. The
learner is never shown them.

**The comparison** (`numbers`) is, over the codes that were heard and that DataSF
gives one kind for: how many Beacon had the right kind for *before* (the five codes
a person wrote down, `beacon.glossary.BUILT_IN`), and how many *after* (those, and
the codes the glossary proposed a kind for). A proposal for a code a person wrote
down is compared too, and never replaces what the person wrote.

**What it does not show.** On generated calls the table counts the incidents
Beacon made of those calls, as they were heard and grouped; the calls were generated
from DataSF's own record of which units were sent to what, so the counts follow
that record and are of nothing heard on a radio. A stand-in's proposals are a rule
of thumb's and say nothing of Muse Ember. And a model can know a fire department's
codes from elsewhere, so a right proposal is not proof that the counts showed it.
Each number says which of these hold for it.

**It is learned once, from the whole day.** The command learns it only when the
run's hearing is complete and closed (`run.close_hearing`), so the counts it was
learned from are of the same evidence as every other number of the run, and they
are kept with it.

**It is paid for once.** The one call is claimed in the run's directory
(`glossary.json`) before Ember is asked, and the claim is given up only when the
paid request is known not to have been sent. A paid run pays under the scope
`scale:glossary`, at most `GLOSSARY_BUDGET_NANOUSD`, the build plan's. **The hold is
the run's**: nothing is asked while the run is leaving Ember alone after a refusal
or a failure, and a refusal or a failure here puts that hold on, in the run's
investigations file where the replay of taps keeps it (`beacon.ingest.paid`).
**What a call did to the hold is written down before the call's outcome is**
(`glossary_hold.json`, beside the glossary), and is put into the investigations file from
there: at once, and by `settle` when that did not happen, which the command calls
before it reads the run's hold. So an answer or a failure is never kept without
the hold it put on.
"""

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from beacon.ember import EmberClient, EmberClosedError, EmberError, LedgerReplacedError
from beacon.errors import raise_bare
from beacon.glossary import (
    BUILT_IN,
    MAX_CODES,
    ROLE_WORDS,
    ROLES,
    Pattern,
    learn_glossary,
)
from beacon.incident.model import Incident
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.paid import Hold, PaidDesk, read_hold
from beacon.ingest.runner import PollerHeldError
from beacon.investigate import Runs
from beacon.radio.vocabulary import clean_unit
from beacon.scale import datasf
from beacon.scale.results import Number
from beacon.scale.run import Layout, ScaleError, Settings, _write

GLOSSARY_SCOPE = "scale:glossary"
GLOSSARY_BUDGET_NANOUSD = 250_000_000
FORMAT = 1
FILE = "glossary.json"
# The run's copy of DataSF's unit types: each unit's types, as a list. A run begun
# by an earlier Beacon has a `unit_types.json` of another shape (one type for a
# unit, and no unit of an unlisted type), which is left where it is and not read:
# such a run fetches the types again, under this name.
TYPES_FILE = "unit_types.2.json"
# What the glossary's call did to the run's hold, until the run's investigations
# file has it.
HOLD_FILE = "glossary_hold.json"
_MAX_BYTES = 1_000_000

# The kind of unit each of DataSF's unit types is, in the glossary's roles. A type
# that is not here is counted as no kind.
TYPE_ROLES = {
    "ENGINE": "engine",
    "TRUCK": "truck",
    "MEDIC": "medic",
    "CHIEF": "chief",
    "RESCUE SQUAD": "rescue_squad",
    "RESCUE CAPTAIN": "rescue_captain",
    "BLS": "ambulance",
    "PRIVATE": "ambulance",
    "CP": "community_paramedic",
    "SUPPORT": "support",
    "INVESTIGATION": "other",
    "AIRPORT": "other",
}
# What a unit's type is kept as when it is none of the above.
UNLISTED = "UNLISTED"


def code_of(unit: str) -> str:
    """The type code of a unit id in its stored form: the letters of `RC2`."""
    return unit.rstrip("0123456789")


def patterns_of(incidents: Sequence[Incident]) -> tuple[list[Pattern], int]:
    """What a day's incidents show of each unit type code, the most named first,
    and how many codes were left out because a call asks about `MAX_CODES`."""
    units: dict[str, set[str]] = {}
    named: Counter[str] = Counter()
    alone: Counter[str] = Counter()
    company: Counter[str] = Counter()
    by_category: dict[str, Counter[str]] = {}
    for incident in incidents:
        codes = {code_of(unit) for unit in incident.units}
        for unit in incident.units:
            units.setdefault(code_of(unit), set()).add(unit)
        for code in codes:
            named[code] += 1
            company[code] += len(incident.units)
            alone[code] += len(codes) == 1
            by_category.setdefault(code, Counter())[incident.category] += 1
    in_order = sorted(named, key=lambda code: (-named[code], code))
    patterns = [
        Pattern(
            code=code,
            units=len(units[code]),
            incidents=named[code],
            by_category=tuple(
                sorted(by_category[code].items(), key=lambda item: (-item[1], item[0]))
            ),
            alone=alone[code],
            mean_units=round(company[code] / named[code], 1),
        )
        for code in in_order
    ]
    return patterns[:MAX_CODES], max(0, len(patterns) - MAX_CODES)


def fetch_unit_types(http: SourceHttp, day: date) -> dict[str, list[str]]:
    """DataSF's types for each unit sent to a call on `day`, by the unit's id in
    its stored form: every type its rows give, each once, in order. A unit whose
    rows give a type Beacon has no kind for, or two types, is still there: it is
    one of its code's units. Only the two columns are asked for.

    Raises SourceError when DataSF cannot be read."""
    body = fetch(
        http,
        "GET",
        datasf.URL,
        source=datasf.SOURCE,
        max_bytes=_MAX_BYTES,
        params={
            "$select": "unit_id,unit_type",
            "$where": f"call_date = '{day.isoformat()}T00:00:00'",
            "$group": "unit_id,unit_type",
            "$limit": "5000",
        },
    )
    listed = read_json(body, datasf.SOURCE)
    if not isinstance(listed, list):
        raise_bare(SourceError(datasf.SOURCE, "sent a reply that is not a list of rows"))
    types: dict[str, set[str]] = {}
    for row in listed:
        unit = clean_unit(row.get("unit_id")) if isinstance(row, dict) else None
        if unit is None:
            continue
        kind = row.get("unit_type")
        # A type that is not one of DataSF's listed ones is kept as no type at all:
        # nothing DataSF wrote freely is stored.
        known = kind.upper() if isinstance(kind, str) and kind.upper() in TYPE_ROLES else UNLISTED
        types.setdefault(unit, set()).add(known)
    return {unit: sorted(kinds) for unit, kinds in sorted(types.items())}


def truth_of(unit_types: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """The kind of unit each code names by DataSF's unit types: the role that more
    than half of all the code's units have. A unit counts for a role only when
    every type DataSF gives it is that one role's (two of DataSF's types can be one
    role: `BLS` and `PRIVATE` are both an ambulance); a unit with a type Beacon has
    no kind for, or with types of two roles, counts for none and is still one of
    the code's units. A code with no such majority has no kind to compare with."""
    units: Counter[str] = Counter()
    votes: dict[str, Counter[str]] = {}
    for unit, kinds in unit_types.items():
        code = code_of(unit)
        units[code] += 1
        roles = {TYPE_ROLES.get(kind) for kind in kinds}
        if len(roles) == 1 and None not in roles:
            (role,) = roles
            assert role is not None
            votes.setdefault(code, Counter())[role] += 1
    truth = {}
    for code, counted in votes.items():
        role, most = counted.most_common(1)[0]
        if 2 * most > units[code]:
            truth[code] = role
    return truth


def prepare_types(layout: Layout, settings: Settings, http: SourceHttp) -> dict[str, list[str]]:
    """The run's copy of DataSF's unit types for its day, fetched once."""
    path = layout.root / TYPES_FILE
    if not path.exists():
        _write(path, fetch_unit_types(http, date.fromisoformat(settings.day)))
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(said, dict) and all(
            isinstance(unit, str)
            and isinstance(kinds, list)
            and kinds
            and all(kind in TYPE_ROLES or kind == UNLISTED for kind in kinds)
            for unit, kinds in said.items()
        ):
            return said
    except (OSError, ValueError):
        pass
    raise ScaleError(f"The run's unit types of DataSF's ({TYPES_FILE}) cannot be read.")


def settle(layout: Layout, runs: Runs) -> None:
    """Put what the glossary's call did to the run's hold into the run's
    investigations file, when an earlier invocation wrote it down and did not get
    that far. The command calls it before it reads the hold, so no tap is replayed
    and no call heard on a hold that is older than the glossary's.

    What is written down is the whole of the hold as it was to be, so putting it
    in twice is putting it in once: one invocation is made at a time, and nothing
    else changes the hold between the writing down and here.

    Raises ScaleError when what was written down cannot be read, and RunsError
    when the investigations file cannot be written."""
    path = layout.root / HOLD_FILE
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        said = None
    if (
        not isinstance(said, dict)
        or set(said) != {"held_until", "unusable_run"}
        # A whole number and nothing that only equals one: `false` and `0.0` are
        # read as no count at all, and would pass the comparison below as zero.
        or type(said["unusable_run"]) is not int
        or Hold(*read_hold(said)).hold_state() != said
    ):
        raise ScaleError(f"The hold the run's glossary put on ({HOLD_FILE}) cannot be read.")
    runs.save_state(said)
    path.unlink()


def _keep(layout: Layout, hold: Hold) -> None:
    """Write down the hold as the glossary's call left it, before the call's
    outcome is kept: `settle` puts it into the investigations file."""
    _write(layout.root / HOLD_FILE, hold.hold_state())


@dataclass(frozen=True)
class Learned:
    """How the glossary of a run stands."""

    # `made`: Ember answered, and what it answered is kept (a glossary, or that the
    # answer was no form). `claimed`: Ember was asked, or may have been, and
    # nothing was kept; it is not asked again. `refused`: the paid request is known
    # not to have been sent in this invocation, and a later one asks. `held`:
    # nothing was asked, because the run is leaving Ember alone; a later
    # invocation asks. `nothing`: the run's incidents name no unit.
    state: str
    patterns: tuple[Pattern, ...] = ()
    # The role proposed for each code that was given one.
    roles: Mapping[str, str] | None = None
    left_out: int = 0
    whole: bool = False
    model: str | None = None
    # What the call cost, when Muse Ember was paid for it in this run. None for a
    # stand-in's, and for an answer that was a recording of an earlier call.
    cost_nanousd: int | None = None
    # Codes named in the day that were not asked about.
    codes_left_out: int = 0
    # The Ember ledger was replaced under the call.
    ledger_lost: bool = False
    # For `refused`: whether the run's hold was put on. It is not when the client
    # was closed before the call, which is no news about Ember.
    hold_set: bool = False

    @property
    def waiting(self) -> bool:
        """Whether the same command, given again, would still ask."""
        return self.state in ("refused", "held")


def learn(
    layout: Layout,
    incidents: Sequence[Incident],
    ember: EmberClient,
    runs: Runs,
    *,
    wall_clock: Callable[[], datetime] | None = None,
) -> Learned:
    """Learn the run's glossary once, or read the one it has. `ember` is closed
    when it returns. `runs` is the run's investigations file, in which the run's
    hold on Ember and its count of answers that were no form are kept;
    `wall_clock` is the time the hold is counted by.

    Raises ScaleError when the run's glossary file cannot be read, and RunsError
    when the hold cannot be put into the investigations file. What the call did
    to the hold is by then written down beside the glossary, and `settle` puts it
    in before the hold is next read."""
    path = layout.root / FILE
    try:
        # A hold an earlier invocation wrote down and did not get into the file.
        settle(layout, runs)
        kept = _read(path)
        if kept is not None:
            return kept
        patterns, more = patterns_of(incidents)
        if not patterns:
            return Learned(state="nothing")
        # A live client pays under the glossary's own scope and no other.
        desk = PaidDesk(
            ember, poller="glossary", items="glossaries", scope=GLOSSARY_SCOPE,
            wall_clock=wall_clock,
        )  # fmt: skip
        hold = Hold(*read_hold(runs.state()))
        try:
            desk.check(hold)
        except PollerHeldError:
            return Learned(state="held", patterns=tuple(patterns), codes_left_out=more)
        asked = [pattern.as_data() for pattern in patterns]
        # The claim, before Ember is asked: a process that dies from here on has
        # maybe paid, and the call is not made a second time.
        _write(path, {"format": FORMAT, "state": "claimed", "patterns": asked, "more": more})
        try:
            glossary = learn_glossary(ember, patterns)
        except EmberError as failure:
            held = not isinstance(failure, EmberClosedError)
            if held:
                # A refusal or a failure: the whole run leaves Ember alone for a
                # while. The hold is written down before the claim is given up.
                desk.hold_after(failure, hold)
                _keep(layout, hold)
            given_up = PaidDesk.not_sent(failure)
            if given_up:
                # No paid request was sent, and nothing was paid: the claim is given up.
                path.unlink(missing_ok=True)
            settle(layout, runs)
            if given_up:
                return Learned(
                    state="refused", patterns=tuple(patterns), codes_left_out=more, hold_set=held
                )
            return Learned(
                state="claimed",
                patterns=tuple(patterns),
                codes_left_out=more,
                ledger_lost=isinstance(failure, LedgerReplacedError),
            )
        if glossary.model != "replay":
            # The answer is counted with the answers the run's taps got: one that
            # is no form adds to the run's count of them, which holds the run when
            # it runs on, and one that is the form ends the count. A recording of
            # an earlier answer is no news about Ember, and is not counted. What
            # the count comes to is written down before the answer is, so that
            # the answer is never kept without it.
            desk.count_answer(hold, usable=glossary.whole)
            _keep(layout, hold)
        # The answer is kept whatever it was: it was asked for, and paid for when
        # Muse Ember gave it.
        _write(
            path,
            {
                "format": FORMAT,
                "state": "made",
                "patterns": asked,
                "more": more,
                "roles": {entry.code: entry.role for entry in glossary.entries},
                "left_out": glossary.left_out,
                "whole": glossary.whole,
                "model": glossary.model,
                "cost_nanousd": glossary.cost_nanousd,
            },
        )
        settle(layout, runs)
        kept = _read(path)
        assert kept is not None
        return kept
    finally:
        ember.close()


def _read(path: Path) -> Learned | None:
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        said = None
    try:
        if said["format"] != FORMAT or said["state"] not in ("claimed", "made"):
            raise ValueError
        patterns = tuple(
            Pattern(
                code=str(row["code"]),
                units=int(row["units"]),
                incidents=int(row["incidents"]),
                by_category=tuple((str(k), int(v)) for k, v in row["by_category"].items()),
                alone=int(row["alone"]),
                mean_units=float(row["mean_units"]),
            )
            for row in said["patterns"]
        )
        more = int(said["more"])
        if said["state"] == "claimed":
            return Learned(state="claimed", patterns=patterns, codes_left_out=more)
        roles = {str(code): str(role) for code, role in said["roles"].items()}
        asked = {pattern.code for pattern in patterns}
        if not all(code in asked and role in ROLES for code, role in roles.items()):
            raise ValueError
        cost = said["cost_nanousd"]
        return Learned(
            state="made",
            patterns=patterns,
            roles=roles,
            left_out=int(said["left_out"]),
            whole=bool(said["whole"]),
            model=str(said["model"]),
            cost_nanousd=None if cost is None else int(cost),
            codes_left_out=more,
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    raise ScaleError("The run's glossary (glossary.json) cannot be read.")


def _who(*, live: bool) -> str:
    return "Muse Ember" if live else "the stand-in for Ember"


def _unasked(*, live: bool) -> str:
    """What a refusal means. A live client counts a request's tokens at Meta before
    it reserves the call's cost, so a refused call can have reached Meta: what was
    not sent is the paid request."""
    if live:
        return "no paid request was sent to Muse Ember and nothing was paid for it"
    return "the stand-in for Ember was not asked"


def waiting_text(learned: Learned, *, live: bool) -> str | None:
    """What a result says of a glossary the same command would still ask for, or
    None when there is nothing left for it to do."""
    if not learned.waiting:
        return None
    who = _who(live=live)
    if learned.state == "held":
        return (
            f"No glossary was asked for yet: the run is leaving {who} alone after a "
            "refusal or a failure, until the hold that was put on has run out. Give "
            "the same command again after that."
        )
    if not learned.hold_set:
        return (
            f"No glossary was learned yet: the client was closed, so {_unasked(live=live)}. "
            "Give the same command again."
        )
    return (
        f"No glossary was learned yet: the call was refused, so {_unasked(live=live)}, "
        "and the run's hold was put on. Give the same command again after the hold "
        "has run out."
    )


def numbers(
    learned: Learned, truth: Mapping[str, str], *, generated: bool, live: bool
) -> list[Number]:
    """The glossary's part of a run's results: each number with its sentence.
    `live` is whether the run asks Muse Ember; without it, a stand-in is asked."""
    if learned.state == "nothing":
        return []
    who = _who(live=live)
    asked_codes = [pattern.code for pattern in learned.patterns]
    checked = [code for code in asked_codes if code in truth]
    # What the table counts, whoever is asked with it and whether or not anybody was.
    table = (
        "The table counts the incidents Beacon made of generated calls, as those "
        "calls were heard and grouped. The calls were generated from DataSF's own "
        "record of which units were sent to what, so the counts follow that record "
        "and are of nothing heard on a radio."
        if generated
        else "The table counts the incidents Beacon made of recorded radio, as the "
        "calls were heard and grouped."
    )
    first = [
        Number(
            "glossary_codes",
            "Unit type codes in the run's incidents",
            len(asked_codes),
            "codes",
            "Different unit type codes (the letters of E36) among the units the run's "
            "incidents name: the codes of the table a glossary is asked for with. "
            f"{learned.codes_left_out} more were named and are not in the table. {table}",
        ),
        Number(
            "glossary_checked",
            "Codes with a majority kind of unit in DataSF",
            len(checked),
            "codes",
            "Of those codes: the ones for which more than half of all the units DataSF "
            "lists for the day under the code have unit types of one kind of unit, "
            "which is taken for the kind the code names. Two of DataSF's types can be "
            "one kind (BLS and PRIVATE are both an ambulance), and a unit with a type "
            "Beacon has no kind for, or with types of two kinds, counts for none. Only "
            "these codes are compared, and nothing checks DataSF's unit types. The "
            "learner is never shown them.",
        ),
        Number(
            "glossary_before",
            "Codes Beacon had the kind of, before",
            sum(BUILT_IN.get(code) == truth[code] for code in checked),
            "codes",
            f"Of those {len(checked)}: the codes among the five a person wrote down "
            "(E, T, B, D, M) whose kind agrees with DataSF's majority kind. This is "
            "what Beacon knows with no glossary.",
        ),
    ]
    cost = _cost(learned, live=live)
    if learned.state != "made" or learned.roles is None or not learned.whole:
        if learned.state == "claimed":
            why = (
                "Not learned: the call was claimed and nothing was kept of it. "
                f"{'Muse Ember' if live else 'The stand-in for Ember'} failed after it "
                "may have been asked, or the run was stopped meanwhile. It is not "
                "asked again."
            )
        elif learned.state == "held":
            why = (
                f"Not asked for yet: the run is leaving {who} alone. The same command, "
                "given again after the hold, asks."
            )
        elif learned.state == "refused":
            again = (
                "given again after the run's hold has run out, asks"
                if learned.hold_set
                else "given again, asks"
            )
            unasked = _unasked(live=live)
            why = f"Not learned yet: {unasked}. The same command, {again}."
        else:
            why = (
                f"Not learned: what {who} answered was not the form, and no glossary "
                "entries were kept. It is not asked again."
            )
        return [
            *first,
            Number("glossary_after", "Codes Beacon has the kind of, after", None, "codes", why),
            *cost,
        ]
    roles = learned.roles
    by = (
        "A stand-in proposed them, by a rule of thumb over the counts: it is no model, "
        "and this says nothing of Muse Ember."
        if learned.model == "fake"
        else "Muse Ember proposed them. A model can know a fire department's codes from "
        "elsewhere, so agreement is not proof that the counts showed it."
    )
    # A proposal is applied only for a code no person wrote down. One for a code a
    # person wrote down is compared and never applied.
    applied = [code for code in checked if code in roles and code not in BUILT_IN]
    considered = [code for code in checked if code in roles and code in BUILT_IN]
    added = [code for code in applied if roles[code] == truth[code]]
    after = sum(BUILT_IN.get(code) == truth[code] for code in checked) + len(added)
    return [
        *first,
        Number(
            "glossary_after",
            "Codes Beacon has the kind of, after",
            after,
            "codes",
            f"Of those {len(checked)}: the codes whose kind agrees with DataSF's "
            "majority kind when the glossary's proposals are applied to the codes no "
            f"person wrote down. A proposal never replaces one of the five. {by} {table}",
        ),
        Number(
            "glossary_new",
            "Codes the glossary added, in agreement with DataSF",
            len(added),
            "codes",
            "Codes that no person wrote down, for which the glossary proposed DataSF's "
            "majority kind"
            + (
                ": "
                + ", ".join(f"{code} ({ROLE_WORDS[roles[code]].lower()})" for code in added)
                + "."
                if added
                else "."
            )
            + f" {by}",
        ),
        Number(
            "glossary_wrong",
            "Codes the glossary added, in disagreement with DataSF",
            len(applied) - len(added),
            "codes",
            "Codes that no person wrote down, for which the glossary proposed a kind "
            "that is not DataSF's majority kind. Applied with nothing to compare them "
            f"with, these would be taken for meanings. {by}",
        ),
        Number(
            "glossary_overruled",
            "Proposals for codes a person wrote down that disagree with DataSF",
            sum(roles[code] != truth[code] for code in considered),
            "codes",
            f"Of {len(considered)} proposals for codes among the five a person wrote "
            "down: those that are not DataSF's majority kind. They are compared and "
            f"never applied: what the person wrote stands. {by}",
        ),
        Number(
            "glossary_unknown",
            "Codes the glossary gave no kind for",
            sum(code not in roles for code in checked),
            "codes",
            'Of the codes that are compared: those answered "unknown", or not at all.',
        ),
        Number(
            "glossary_left_out",
            "Entries of the answer that were not kept",
            learned.left_out,
            "entries",
            "Entries for a code that was not asked about, with a role that is not on "
            "the list, or a second one for a code. They are left out, never mended.",
        ),
        *cost,
    ]


def _cost(learned: Learned, *, live: bool) -> list[Number]:
    """What the one call cost, for a call that was answered: whether or not the
    answer was the form, a paid answer was paid for."""
    if learned.state != "made":
        return []
    label = "Cost of asking for the glossary"
    if learned.model == "fake":
        text = (
            "Not measured: a stand-in was asked, nothing was paid, and no cost of "
            "Muse Ember's is known from this run."
        )
        return [Number("glossary_cost_usd", label, None, "USD", text)]
    if learned.cost_nanousd is None:
        text = (
            "Not measured in this run: the answer was not a live Ember's own, but a "
            "recording of an earlier call with the same table."
        )
        return [Number("glossary_cost_usd", label, None, "USD", text)]
    text = (
        "What Muse Ember's one answer for this run's glossary cost, by the Ember "
        f"client's accounting of that call, paid under the scope {GLOSSARY_SCOPE}, "
        "whether or not the answer was the form. It is the cost of one table of "
        "counts, and no cost of hearing radio."
    )
    return [Number("glossary_cost_usd", label, learned.cost_nanousd / 1e9, "USD", text)]
