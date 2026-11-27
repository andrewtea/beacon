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

**What it does not show.** On generated calls the incidents repeat DataSF's own
record of which units were sent to what, so the pattern is DataSF's dispatch
pattern and not what Beacon heard on a radio. A stand-in's proposals are a rule of
thumb's and say nothing of Muse Ember. And a model can know a fire department's
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
TYPES_FILE = "unit_types.json"
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


def fetch_unit_types(http: SourceHttp, day: date) -> dict[str, str]:
    """DataSF's type for each unit sent to a call on `day`, by the unit's id in its
    stored form. Only the two columns are asked for.

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
    types: dict[str, str] = {}
    for row in listed:
        unit = clean_unit(row.get("unit_id")) if isinstance(row, dict) else None
        kind = row.get("unit_type") if isinstance(row, dict) else None
        if unit is not None and isinstance(kind, str) and kind.upper() in TYPE_ROLES:
            types[unit] = kind.upper()
    return types


def truth_of(unit_types: Mapping[str, str]) -> dict[str, str]:
    """The kind of unit each code names by DataSF's unit types: the role more than
    half of the code's units have. A code whose units are of several kinds, none of
    them of more than half, has none."""
    roles: dict[str, Counter[str]] = {}
    for unit, kind in unit_types.items():
        roles.setdefault(code_of(unit), Counter())[TYPE_ROLES[kind]] += 1
    truth = {}
    for code, counted in roles.items():
        role, most = counted.most_common(1)[0]
        if 2 * most > sum(counted.values()):
            truth[code] = role
    return truth


def prepare_types(layout: Layout, settings: Settings, http: SourceHttp) -> dict[str, str]:
    """The run's copy of DataSF's unit types for its day, fetched once."""
    path = layout.root / TYPES_FILE
    if not path.exists():
        _write(path, fetch_unit_types(http, date.fromisoformat(settings.day)))
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(said, dict) and all(
            isinstance(unit, str) and kind in TYPE_ROLES for unit, kind in said.items()
        ):
            return said
    except (OSError, ValueError):
        pass
    raise ScaleError("The run's unit types of DataSF's (unit_types.json) cannot be read.")


@dataclass(frozen=True)
class Learned:
    """How the glossary of a run stands."""

    # `made`: a glossary was learned and kept. `claimed`: Ember was asked, or may
    # have been, and nothing was kept; it is not asked again. `refused`: the paid
    # request is known not to have been sent in this invocation, and a later one
    # asks. `held`: nothing was asked, because the run is leaving Ember alone;
    # a later invocation asks. `nothing`: the run's incidents name no unit.
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
    hold on Ember is kept; `wall_clock` is the time the hold is counted by.

    Raises ScaleError when the run's glossary file cannot be read, and RunsError
    when the hold cannot be kept."""
    path = layout.root / FILE
    try:
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
            if not isinstance(failure, EmberClosedError):
                # A refusal or a failure: the whole run leaves Ember alone for a while.
                desk.hold_after(failure, hold)
                runs.save_state(hold.hold_state())
            if PaidDesk.not_sent(failure):
                # Nothing was sent and nothing was paid: the claim is given up.
                path.unlink(missing_ok=True)
                return Learned(state="refused", patterns=tuple(patterns), codes_left_out=more)
            return Learned(
                state="claimed",
                patterns=tuple(patterns),
                codes_left_out=more,
                ledger_lost=isinstance(failure, LedgerReplacedError),
            )
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


def waiting_text(learned: Learned, *, live: bool) -> str | None:
    """What a result says of a glossary the same command would still ask for, or
    None when there is nothing left for it to do."""
    if not learned.waiting:
        return None
    who = "Muse Ember" if live else "the stand-in for Ember"
    if learned.state == "held":
        return (
            f"The glossary is not learned yet: the run is leaving {who} alone after a "
            "refusal or a failure, until the hold that was put on has run out, and "
            "nothing was asked. Give the same command again after that."
        )
    return (
        "The glossary is not learned yet: the call was refused before anything was "
        f"sent to {who}, by its spending scope or the spending stop, so nothing was "
        "paid for it. The run leaves it alone until the hold that was put on has run "
        "out. Give the same command again after that."
    )


def numbers(learned: Learned, truth: Mapping[str, str], *, generated: bool) -> list[Number]:
    """The glossary's part of a run's results: each number with its sentence."""
    if learned.state == "nothing":
        return []
    heard = [pattern.code for pattern in learned.patterns]
    checked = [code for code in heard if code in truth]
    of_pattern = (
        "The counts it was learned from are of generated calls, which repeat DataSF's "
        "own record of which units were sent to what: they are DataSF's dispatch "
        "pattern, not what Beacon heard on a radio."
        if generated
        else "The counts it was learned from are of the incidents Beacon made of recorded radio."
    )
    asked = [
        Number(
            "glossary_codes",
            "Unit type codes in the run's incidents",
            len(heard),
            "codes",
            "Different unit type codes (the letters of E36) among the units the run's "
            "incidents name, which are the codes the glossary is asked about. "
            f"{learned.codes_left_out} more were named and not asked about. " + of_pattern,
        ),
        Number(
            "glossary_checked",
            "Codes DataSF gives one kind of unit for",
            len(checked),
            "codes",
            "Of those codes: the ones for which more than half of the units DataSF "
            "lists for the day have one unit type, which is taken for the kind of unit "
            "the code names. Only these are compared, and nothing checks DataSF's "
            "unit types. The learner is never shown them.",
        ),
        Number(
            "glossary_before",
            "Codes Beacon had the kind of, before",
            sum(BUILT_IN.get(code) == truth[code] for code in checked),
            "codes",
            f"Of those {len(checked)}: the codes among the five a person wrote down "
            "(E, T, B, D, M) whose kind agrees with DataSF's unit type. This is what "
            "Beacon knows with no glossary.",
        ),
    ]
    if learned.state != "made" or learned.roles is None or not learned.whole:
        why = {
            "claimed": (
                "Not learned: the call was claimed and no glossary was kept. Ember "
                "failed after it may have been asked, or the run was stopped "
                "meanwhile. It is not asked again."
            ),
            "refused": (
                "Not learned yet: the call was refused before anything was sent, so "
                "nothing was paid. The same command, given again after the run's "
                "hold, asks."
            ),
            "held": (
                "Not learned yet: nothing was asked while the run is leaving Ember, or "
                "its stand-in, alone. The same command, given again after the hold, asks."
            ),
        }.get(learned.state, "Not learned: the answer was not the form, and none of it is kept.")
        return [
            *asked,
            Number("glossary_after", "Codes Beacon has the kind of, after", None, "codes", why),
        ]
    roles = learned.roles
    stand_in = learned.model == "fake"
    who = (
        "A stand-in proposed them, by a rule of thumb over the counts: it is no model, "
        "and this says nothing of Muse Ember."
        if stand_in
        else "Muse Ember proposed them. A model can know a fire department's codes from "
        "elsewhere, so agreement is not proof that the counts showed it."
    )
    after = {code: BUILT_IN.get(code) or roles.get(code) for code in checked}
    proposed = [code for code in checked if code in roles]
    agree = [code for code in proposed if roles[code] == truth[code]]
    new = [code for code in agree if code not in BUILT_IN]
    out = [
        *asked,
        Number(
            "glossary_after",
            "Codes Beacon has the kind of, after",
            sum(after[code] == truth[code] for code in checked),
            "codes",
            f"Of those {len(checked)}: the codes whose kind agrees with DataSF's unit "
            "type when the glossary's proposals are added to the five a person wrote "
            f"down, which a proposal never replaces. {who} {of_pattern}",
        ),
        Number(
            "glossary_new",
            "Codes the glossary added, in agreement with DataSF",
            len(new),
            "codes",
            "Codes that no person wrote down, for which the glossary proposed the "
            "kind of unit DataSF's unit type gives"
            + (
                ": "
                + ", ".join(f"{code} ({ROLE_WORDS[roles[code]].lower()})" for code in new)
                + "."
                if new
                else "."
            ),
        ),
        Number(
            "glossary_wrong",
            "Proposals that disagree with DataSF",
            len(proposed) - len(agree),
            "codes",
            "Codes the glossary proposed a kind for that is not the one DataSF's unit "
            "type gives, the five a person wrote down among them. Such a proposal "
            "would be shown as a meaning if nothing compared it.",
        ),
        Number(
            "glossary_unknown",
            "Codes the glossary gave no kind for",
            len(checked) - len(proposed),
            "codes",
            'Of the codes that can be compared: those answered "unknown", or not at all.',
        ),
        Number(
            "glossary_left_out",
            "Entries of the answer that were not kept",
            learned.left_out,
            "entries",
            "Entries for a code that was not asked about, with a role that is not on "
            "the list, or a second one for a code. They are left out, never mended.",
        ),
    ]
    if stand_in:
        cost = Number(
            "glossary_cost_usd",
            "Cost of learning the glossary",
            None,
            "USD",
            "Not measured: a stand-in proposed the glossary, nothing was paid, and no "
            "cost of Muse Ember's is known from this run.",
        )
    elif learned.cost_nanousd is None:
        cost = Number(
            "glossary_cost_usd",
            "Cost of learning the glossary",
            None,
            "USD",
            "Not measured in this run: the answer was not a live Ember's own, but a "
            "recording of an earlier call with the same table.",
        )
    else:
        cost = Number(
            "glossary_cost_usd",
            "Cost of learning the glossary",
            learned.cost_nanousd / 1e9,
            "USD",
            "What Muse Ember's one call for this run's glossary cost, by the Ember "
            f"client's accounting of that call, paid under the scope {GLOSSARY_SCOPE}. "
            "It is the cost of one table of counts, and no cost of hearing radio.",
        )
    return [*out, cost]
