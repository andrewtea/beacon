"""What a scale run came to, in numbers that each say what they are.

**A number a stand-in produced is not a measurement of Ember, and a number from
generated audio is not a measurement of radio.** So nothing here is a bare number.
A run's results begin with what the run is (`basis`): what its audio was, who heard
it, and what follows for everything below. Each number (`Number`) carries a sentence
written here, by code, that says what was counted and, where it matters, what the
count does not show. Whoever shows a result shows `basis_text` with it and each
number's `text` beside the number; the results page and the command line both do.

Three kinds of run, told apart by the run's own settings (`beacon.scale.run`):

- **generated calls, heard by a stand-in**: no radio, and no Ember. The stand-in
  "hears" each call by looking it up in the generator's script, so it is right by
  construction. The run shows that the pipeline keeps what it is given: nothing of
  Beacon's accuracy, speed, or cost. It reports no cost, no latency, and no lead
  time, because it has none to report.
- **generated speech, heard by Muse Ember**: Ember is paid, so the cost is a real
  cost, of clean synthetic speech. The calls were generated from the records they
  are scored against, and a match is a fit by rule between a record and an
  incident: it is no proof that Ember made out a call (a record whose call was
  never generated can fit too), and not that Beacon finds a real city's calls.
- **recorded radio, heard by Muse Ember**: the one kind whose numbers are about
  Beacon on real radio, scored against DataSF's record of the same day.

What is not measured in any run: whether what an incident says is right beyond its
kind and its place, and anything about calls DataSF does not list.
"""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from beacon.ember.ledger import ScopeSpend
from beacon.ember.pricing import to_usd
from beacon.ingest.recordings import PassCount
from beacon.investigate import TAP_BUDGET_USD, Run
from beacon.scale import datasf
from beacon.scale.generate import BLOCK_REACH_M
from beacon.scale.run import (
    RADIO_SCOPE,
    SPEECH_SCOPE,
    TAPS_SCOPE,
    Layout,
    ScaleError,
    Settings,
)
from beacon.scale.score import NEAR_M, Matched, Reading, first_heard
from beacon.scale.taps import Replay, Standing

FORMAT = 1

# What a run is, said before any of its numbers. Each is about what the run is set up
# to do, whatever has been done of it so far: a run that has heard nothing yet, on
# an empty shelf or at its first refusal, begins with the same sentence, and the
# numbers below it say how many calls were heard and what was paid.
GENERATED_BY_STAND_IN = (
    "Stand-in run on generated calls. The calls are generated from DataSF's own "
    'records of this day, and a stand-in "hears" each by looking it up in the '
    "generator's script. No radio is heard and Muse Ember is asked nothing. "
    "Nothing below measures Beacon's accuracy, speed, or cost on real radio: it shows "
    "only that the pipeline keeps what it is given."
)
GENERATED_BY_EMBER = (
    "Generated speech, for Muse Ember to hear. The calls are synthesized speech "
    "generated from DataSF's own records of this day, and Muse Ember is paid for each "
    "one it is asked to hear; the numbers below say how many that was. They are scored "
    "against the records they were generated from. A match is a fit by rule between "
    "a record and an incident, and is no proof that Ember made out a call: a record "
    "whose call was never generated can fit an incident too. Nothing below measures "
    "Beacon on real radio, which is noisy, clipped, and says things no record holds."
)
RECORDED_BY_EMBER = (
    "Recorded radio, for Muse Ember to hear. The calls are recordings of dispatch "
    "radio that the run's operator said they have the right to use. Muse Ember is "
    "paid for each one it is asked to hear; the numbers below say how many that was. "
    "What Beacon makes of them is scored against DataSF's record of the same day."
)
# What the matching rule does not check. Of recorded radio it also cannot tell a
# call DataSF does not list from a wrong incident; generated calls are each of a
# record, so nothing is said of an unlisted call there.
NOT_CHECKED_GENERATED = (
    "A match is by kind, time, and unit or place. Nothing checks what an incident says beyond that."
)
NOT_CHECKED = (
    "A match is by kind, time, and unit or place. Nothing checks what an incident "
    "says beyond that, and a call DataSF does not list cannot be told from a wrong "
    "incident."
)
INCOMPLETE = (
    "The hearing is not complete: calls are waiting to be heard, or were not ready. "
    "The numbers below are of what was heard so far."
)
CLOSED = (
    "The hearing was closed when it was first complete, so that whatever is made of "
    "all the run's calls (answers to replayed taps, a glossary) is of the same "
    "evidence: a call put among the run's calls after that is not heard in this run."
)


def _sample_basis(calls: int, of: int) -> str:
    """What is added to what a run is when it is of a sample of a day's calls: what
    is the sample's, and the three things among the numbers that are not."""
    return (
        f" This run is of a sample: the first {calls} calls of the day, in the order "
        f"they were dispatched, of the {of} in DataSF's record of the day that Beacon "
        "could read. Only those were generated and are scored, and replayed taps are "
        "planned from them alone, so the counts of calls, incidents, places, and taps "
        "below are of that sample and of no whole day; a day's first calls need not "
        "be like the rest of it. Three things below are not the sample's, and each "
        "says so: what of DataSF's answer for the whole day could not be read, "
        "DataSF's unit types, which a glossary is compared with and which are the "
        "whole day's, and a cost of hearing, which is its spending scope's over "
        "every paid run."
    )


def _sample_label(calls: int) -> str:
    return f", sample of {calls} calls"


def _sample_reminder(calls: int, of: int) -> str:
    """What goes under every heading of a sampled run's numbers. A heading can hold
    a number that is not the sample's (what of DataSF's whole answer could not be
    read, the whole day's unit types, a scope's cost), so it claims the sample's
    for none that says otherwise of itself."""
    return (
        f" It is of a sample, the first {calls} of the {of} calls Beacon could read "
        "for that day, and of no whole day: a number here is of the sample unless "
        "its own sentence says it is of the whole day or of a spending scope."
    )


def _sample_of(results: Mapping[str, Any]) -> tuple[int, int] | None:
    """The sample a written result says it is of, as how many calls of how many,
    or None for a result of a whole day.

    Raises ValueError for a sample that is none a run can be of."""
    said = results.get("sample")
    if said is None:
        return None
    calls, of = (said.get("calls"), said.get("of")) if isinstance(said, dict) else (None, None)
    counts = all(isinstance(n, int) and not isinstance(n, bool) for n in (calls, of))
    # A sample is fewer calls than its day has: a run of them all is a day's.
    if not counts or not 1 <= calls < of or results.get("audio") != "generated":
        raise ValueError("The result's sample is none a run can be of.")
    return calls, of


LEDGER_LOST = (
    "The Ember ledger was replaced while the run was under way, so what the run "
    "spent is not known from it and no cost of hearing is reported. Compare Meta's "
    "Usage page with the ledger before any more paid work."
)


def who(*, live: bool) -> str:
    """Who a run asks: Muse Ember, which is paid, or the stand-in, which is not."""
    return "Muse Ember" if live else "the stand-in for Ember"


# A hold is ten minutes from the failure or longer (as long as Meta asked, and never
# shorter than one that was already on), and a result is read long after it was
# written: so a result names no length for one, and the command says how long the
# hold has to run when it is given.
_UNTIL = "until the hold that was put on has run out"


def _taps_stopped(stopped: str | None, *, live: bool) -> str:
    """Why a replay stopped, in words that name who was asked."""
    asked = who(live=live)
    paid = ", each of them paid for" if live else ""
    return {
        "held": (
            f"It is leaving {asked} alone after a refusal or a failure, {_UNTIL}. "
            "Give the same command again after that."
        ),
        "unusable": (
            f"Answers of {asked}'s were not the form, several times in a row{paid}. "
            f"It is asking nothing more {_UNTIL}."
        ),
        "budget": (
            "Its spending scope has less left than one tap may cost, or the spending "
            "stop refused. The taps that were not begun are not claimed."
        ),
        "ledger_replaced": "The Ember ledger was replaced under it.",
        "ember": f"A call to {asked} failed. It is asking nothing more {_UNTIL}.",
        "store": "The investigations file could not be written.",
    }.get(stopped or "", "The replay stopped early.")


PARTIAL_READING = "The incident engine left rows out of a reading, so an incident can lack calls."


def _stopped(stopped: str, *, live: bool) -> str:
    """Why a hearing stopped, in words that name who was asked."""
    asked = who(live=live)
    return {
        "budget": "The run stopped at its spending cap or the spending stop.",
        "held": (
            f"The run stopped because it is leaving {asked} alone after a refusal or "
            f"a failure, {_UNTIL}. Give the same command again after that."
        ),
        "ledger_replaced": (
            "The run stopped because the Ember ledger was replaced under it. Compare "
            "Meta's Usage page with the ledger before anything more is paid for."
        ),
        "ember": f"The run stopped because a call to {asked} failed.",
        "source": "The run stopped because the directory of calls could not be read.",
        "store": "The run stopped because its evidence store could not be written.",
    }.get(stopped, "The run stopped early.")


@dataclass(frozen=True)
class Number:
    """One number of a result, with the sentence that says what it is."""

    key: str
    # What is counted, in a few words.
    label: str
    # None when the run has no such number, which `text` then says and why.
    value: float | int | None
    unit: str
    # What was counted and what the count does not show. Shown beside the number.
    text: str
    # The heading the number is shown under, which `build` gives it: one of `SECTIONS`.
    section: str = ""

    def as_data(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "text": self.text,
            "section": self.section,
        }


# The headings a result's numbers are shown under, in their order. A heading names
# what its numbers are about and claims nothing.
RECORD = "DataSF's record of the day"
HEARD = "Calls heard"
MATCHED = "Calls matched to DataSF's record"
PLACE = "Place"
TIME = "Time"
COST = "Cost of hearing"
TAPS = "Replayed taps"
GLOSSARY = "Glossary"
SECTIONS = (RECORD, HEARD, MATCHED, PLACE, TIME, COST, TAPS, GLOSSARY)


def _under(section: str, numbers: Sequence[Number]) -> list[Number]:
    return [replace(number, section=section) for number in numbers]


def shown(number: Mapping[str, Any]) -> str:
    """A number's value as it is shown, with its unit: `468 calls`, `85.2%`, or
    `none` for a number the run does not have, which its sentence then explains."""
    value = number["value"]
    return "none" if value is None else _shown(value, number["unit"])


# What a run is in a few words, for a label beside each of its numbers; the sentence
# that goes with every group of them wherever they are shown apart from
# `basis_text`, so that a number read by itself is not taken for Beacon's on real
# radio; and the `basis_text` a run of that kind is written with. Like the basis,
# each is about what the run is set up to do, whatever has been done of it so far.
_KINDS: dict[tuple[str, str], tuple[str, str | None, str]] = {
    ("generated", "fake"): (
        "Stand-in, generated calls",
        "A stand-in's numbers on calls generated from DataSF's record: a check that "
        "the pipeline keeps what it is given, and no measurement of Beacon's accuracy, "
        "speed, or cost on real radio.",
        GENERATED_BY_STAND_IN,
    ),
    ("generated", "live"): (
        "Muse Ember, generated speech",
        "This run is set up for Muse Ember to hear speech generated from DataSF's "
        "record, and is scored against the record the speech was generated from: its "
        "numbers are no measurement of Beacon on real radio.",
        GENERATED_BY_EMBER,
    ),
    ("recorded", "live"): ("Muse Ember, recorded radio", None, RECORDED_BY_EMBER),
}


def kind(results: Mapping[str, Any]) -> tuple[str, str | None]:
    """What a written result's run is, in a few words, and the sentence shown with
    every group of its numbers, or None for the one kind of run whose numbers are
    about Beacon on real radio.

    Raises KeyError for a result that is of no kind of run Beacon makes, and
    ValueError for one that does not say of itself what a run of its kind is: its
    `basis_text` is another kind's, or its `stand_in` is not what its kind makes it.
    Such a result is not relabeled; it is not shown."""
    label, reminder, said = _KINDS[results["audio"], results["hearer"]]
    sample = _sample_of(results)
    if sample is not None:
        # A sample is said wherever the kind is: beside every number, under every
        # heading, and in the words the result begins with.
        assert reminder is not None
        label += _sample_label(sample[0])
        reminder += _sample_reminder(*sample)
        said += _sample_basis(*sample)
    if results["basis_text"] != said:
        raise ValueError("The result's basis is not its kind's.")
    stand_in = results["stand_in"]
    if not isinstance(stand_in, bool) or stand_in != (reminder is not None):
        raise ValueError("The result does not say what a stand-in had a part in.")
    return label, reminder


def basis(settings: Settings, *, day_calls: int | None = None) -> str:
    """What a run is, and what follows for its numbers. `day_calls` is how many
    calls of the day Beacon could read in DataSF's record, which a run of a sample
    is said with."""
    if settings.audio == "recorded":
        return RECORDED_BY_EMBER
    said = GENERATED_BY_EMBER if settings.hearer == "live" else GENERATED_BY_STAND_IN
    if settings.sample is None:
        return said
    if day_calls is None:
        raise ValueError("A sample is said with how many calls the day has.")
    if not 1 <= settings.sample < day_calls:
        raise ValueError("A sample is fewer calls than the day has.")
    return said + _sample_basis(settings.sample, day_calls)


def build(
    name: str,
    settings: Settings,
    records: Sequence[datasf.Record],
    *,
    unread: datasf.Unread | None = None,
    day_calls: int | None = None,
    rows: int,
    no_row: int,
    listed: int | None,
    unplaced: int | None = None,
    last_pass: PassCount | None,
    stopped: str | None,
    closed: bool = False,
    reading: Reading,
    matched: Matched,
    radio_spend: ScopeSpend | None,
    ledger_lost: bool = False,
    taps: Sequence[tuple[Replay, Standing, Run | None]],
    taps_stopped: str | None,
    taps_plan: str | None = None,
    glossary: Sequence[Number] = (),
    glossary_waiting: str | None = None,
    made_at: datetime | None = None,
) -> dict[str, Any]:
    """A run's results, as they are written and shown. `rows` is how many radio
    rows the run's store holds, `no_row` how many calls are claimed there with no
    row, and `listed` how many calls there are to hear, when that is known;
    `unplaced` is how many records the generator found no block for;
    `day_calls` is how many calls of the day Beacon could read, for a run of a
    sample, whose `records` are the sample's alone;
    `last_pass` and `stopped` are how the latest hearing ended, `closed` whether
    the hearing was closed for the replay, `unread` what of DataSF's answer could
    not be read, and `ledger_lost` whether the Ember ledger was replaced under the
    run. `glossary` is the glossary's numbers (`beacon.scale.glossary.numbers`), and
    `glossary_waiting` the sentence for a glossary the same command would still ask
    for, with which a run is not complete.
    `taps` is every planned tap with how it stands, or nothing while the hearing
    is not complete."""
    live = settings.hearer == "live"
    generated = settings.audio == "generated"
    sample: tuple[int, int] | None = None
    if settings.sample is not None:
        if day_calls is None or not 1 <= len(records) < day_calls:
            raise ValueError("A sample is fewer calls than the day has, and says how many.")
        sample = (len(records), day_calls)
    sampled = sample is not None
    heard_complete = stopped is None and last_pass is not None and last_pass.unready == 0
    waiting = sum(state == "waiting" for _, state, _ in taps)
    # A run is complete when nothing is left that the same command would still do:
    # a pass over the calls ended with none of them unready, the replay did not
    # stop, and no planned tap is waiting. It does not say that every call has a
    # row or every tap an answer: a call or a tap that was claimed and came to
    # nothing is counted in its own number, and is not done again.
    complete = (
        heard_complete
        and taps_stopped is None
        and waiting == 0
        and glossary_waiting is None
        and not ledger_lost
    )
    caveats = [NOT_CHECKED_GENERATED if generated else NOT_CHECKED]
    if not heard_complete:
        caveats.insert(0, INCOMPLETE)
    if stopped is not None:
        caveats.insert(1, _stopped(stopped, live=live))
    if closed:
        caveats.append(CLOSED)
    if ledger_lost:
        caveats.append(LEDGER_LOST)
    if reading.partial:
        caveats.append(PARTIAL_READING)
    numbers = [
        *_under(RECORD, _record_numbers(records, unread or datasf.Unread(), sample)),
        *_under(HEARD, _hearing_numbers(rows, no_row, listed, last_pass, live=live)),
        *_under(
            MATCHED,
            _match_numbers(
                records, reading, matched, generated=generated, live=live, sampled=sampled
            ),
        ),
        *_under(PLACE, _place_numbers(matched, generated=generated, live=live, unplaced=unplaced)),
        *_under(TIME, _time_numbers(matched, generated=generated)),
        *_under(COST, _cost_numbers(radio_spend, live=live, generated=generated, lost=ledger_lost)),
        *_under(
            TAPS,
            _tap_numbers(taps, reading, matched, live=live, generated=generated, sampled=sampled),
        ),
        *_under(GLOSSARY, glossary),
    ]
    if taps_stopped is not None or waiting:
        caveats.append(
            f"The replay of taps is not complete: {waiting} planned taps have not been "
            "put to the investigator. " + _taps_stopped(taps_stopped, live=live)
        )
    if glossary_waiting is not None:
        caveats.append(glossary_waiting)
    return {
        "format": FORMAT,
        "name": name,
        "made_at": (datetime.now(UTC) if made_at is None else made_at).isoformat(),
        "day": settings.day,
        "audio": settings.audio,
        "voice": settings.voice,
        "hearer": settings.hearer,
        # Whether a stand-in had a part in the numbers: generated audio, a stand-in
        # for Ember, or both. Only recorded radio heard by Muse Ember is neither.
        "stand_in": generated or not live,
        # Whether a pass over the calls ended with none unready, and then whether
        # nothing is left for the same command to do: no planned tap waiting, the
        # replay not stopped, and the ledger the one the run began in.
        "heard_complete": heard_complete,
        "complete": complete,
        # The plan of taps the result is of (`taps.plan_name`): the command given
        # again with another plan has more to do than this result says.
        "taps_plan": taps_plan,
        # For a run of a sample: how many calls it is of, of how many the day has.
        # Null for a run of the whole day.
        "sample": None if sample is None else {"calls": sample[0], "of": sample[1]},
        "basis_text": basis(settings, day_calls=day_calls),
        "caveats": caveats,
        "scored_against": {
            "text": datasf.CREDIT,
            "url": datasf.PAGE_URL,
        },
        "numbers": [number.as_data() for number in numbers],
    }


def write(layout: Layout, results: dict[str, Any]) -> None:
    """Keep a run's results, whole."""
    beside = layout.results.with_name(layout.results.name + ".part")
    beside.write_text(json.dumps(results, indent=1), encoding="utf-8")
    os.replace(beside, layout.results)


def read(layout: Layout) -> dict[str, Any] | None:
    """A run's results as they were written, or None when it has none.

    Raises ScaleError when they are not results of the shape this Beacon writes."""
    try:
        said = json.loads(layout.results.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        said = None
    if not isinstance(said, dict) or said.get("format") != FORMAT:
        raise ScaleError("The run's results (results.json) are not in a shape Beacon reads.")
    return said


def lines(results: dict[str, Any]) -> list[str]:
    """A result as lines for a terminal: what the run is first, and every number
    with its sentence."""
    out = [f"Scale run {results['name']}, {results['day']}", "", results["basis_text"], ""]
    out += [f"Note: {caveat}" for caveat in results["caveats"]]
    out.append(f"Scored against: {results['scored_against']['text']}")
    out.append("")
    # Whose number it is, beside each number, as the page has it: a line read by
    # itself, or copied out, still says what kind of run it is of.
    label, reminder = kind(results)
    whose = "" if reminder is None else f"  [{label}]"
    for number in results["numbers"]:
        out.append(f"{number['label']}: {shown(number)}{whose}")
        out.append(f"    {number['text']}")
    return out


def _record_numbers(
    records: Sequence[datasf.Record],
    unread: datasf.Unread,
    sample: tuple[int, int] | None = None,
) -> list[Number]:
    medical = sum(record.medical for record in records)
    kept = (
        f"{medical} of them are medical, of which Beacon keeps DataSF's number for "
        "the call, the crews, the intersection, and the times, and nothing else."
    )
    of_day = Number(
        "records",
        "DataSF calls Beacon could read",
        len(records),
        "calls",
        "Calls the fire department was dispatched to on this day, by DataSF's "
        "record, that have a number, a type, and the times they were received and "
        f"dispatched. Every count and share below is of these. {kept}",
    )
    if sample is not None:
        of_day = Number(
            "records",
            "DataSF calls in the sample",
            len(records),
            "calls",
            f"The first {sample[0]} calls the fire department was dispatched to on this "
            f"day, in the order they were dispatched, of the {sample[1]} in DataSF's "
            "record that have a number, a type, and the times they were received and "
            "dispatched. The counts and shares of calls below are of these and not "
            f"of the day. {kept}",
        )
    # What could not be read is of DataSF's whole answer, whatever the run is of.
    whole = " This is of DataSF's answer for the whole day, not of the sample." if sample else ""
    return [
        of_day,
        Number(
            "records_unreadable",
            "DataSF calls Beacon could not read",
            unread.calls,
            "calls",
            "Calls in DataSF's answer for the day, each with a number of its own, that "
            "lack a type or the time they were received or dispatched. They are in no "
            "count below, so the calls above are not all the calls DataSF lists when "
            f"this is not 0.{whole}",
        ),
        Number(
            "rows_unreadable",
            "DataSF rows of no call Beacon can tell",
            unread.rows,
            "rows",
            "Rows of DataSF's answer, each of which is one unit sent to one call, that "
            "are not shaped as a row or have no call number Beacon reads. Several can "
            "be of one call, so this is a count of rows and not of calls; whatever "
            f"calls they are of may be missing from every count below.{whole}",
        ),
    ]


def _hearing_numbers(
    rows: int, no_row: int, listed: int | None, last_pass: PassCount | None, *, live: bool
) -> list[Number]:
    who = "Muse Ember" if live else "the stand-in"
    of = "" if listed is None else f", of {listed} there are to hear"
    numbers = [
        Number(
            "transmissions",
            "Transmissions heard",
            rows,
            "calls",
            f"Radio calls that {who} heard and Beacon kept a row for, over every "
            f"invocation of this run{of}. Each is heard once.",
        ),
        Number(
            "no_row",
            "Calls claimed with no row kept",
            no_row,
            "calls",
            f"Calls that were claimed for {who} to hear and that Beacon has no row "
            "for: the answer was no form, the request failed after it may have been "
            "sent, or the run was stopped between the claim and the row. A claim is "
            "for good, so none of them is asked about, or paid for, again."
            + (" Each may have been paid for." if live else ""),
        ),
    ]
    if last_pass is not None:
        numbers.append(
            Number(
                "unready",
                "Calls not ready",
                last_pass.unready,
                "calls",
                "Calls whose files could not be used as they were in the last pass. "
                "Nobody claimed them, nothing was paid, and the next pass looks again.",
            )
        )
        numbers.append(
            Number(
                "unusable",
                "Calls that came to nothing",
                last_pass.unusable,
                "calls",
                f"Calls in the last pass that {who} was asked about and whose answer "
                "was no form. They are not asked about again.",
            )
        )
    return numbers


def _match_numbers(
    records: Sequence[datasf.Record],
    reading: Reading,
    matched: Matched,
    *,
    generated: bool,
    live: bool,
    sampled: bool = False,
) -> list[Number]:
    if generated and not live:
        meaning = (
            "The calls were generated from these records and a stand-in read them "
            "back, so this is a check of the pipeline, not a measure of accuracy."
        )
    elif generated:
        meaning = (
            "The calls were generated from these records and are clean synthetic "
            "speech, so this is no measure of Beacon on real radio; and a fit by the "
            "rule is no proof that Ember made out the call it is counted for."
        )
    else:
        meaning = "This is Beacon on recorded radio, by the matching rule stated above."
    share = None if not records else round(100 * len(matched.found) / len(records), 1)
    silent = []
    if generated:
        unheard = {record.number for record in records if not record.units}
        found_by_chance = sum(pair.record.number in unheard for pair in matched.found)
        by_chance = sum(record.number in unheard for record in matched.merged)
        silent = [
            Number(
                "not_generated",
                "DataSF calls no call was generated for",
                len(unheard),
                "calls",
                "DataSF calls that name no unit whose id is shaped as Beacon keeps one "
                "(a type code and a number), so the generator made no call for them "
                f"and nothing was heard of them. {found_by_chance} of them are counted "
                f"above as found and {by_chance} as taken to be inside another's "
                "incident, which for a call nobody generated is chance: an incident of "
                "another call's was near it at the time. That is how loose both "
                "counts are.",
            )
        ]
    wrong_kind = sum(not pair.same_kind for pair in matched.found)
    return [
        Number(
            "incidents",
            "Incidents Beacon made",
            len(reading.incidents),
            "incidents",
            "Incidents the incident engine made of the run's radio rows.",
        ),
        Number(
            "found",
            "DataSF calls found",
            len(matched.found),
            "calls",
            "DataSF calls that were paired with an incident of Beacon's, one to one: "
            "of the same medical-or-not kind, opened within the call's time, and "
            f"naming one of its units or placed within {NEAR_M:.0f} m of it. A pair "
            f"is a fit by that rule, and nothing checks that it is right. {meaning}",
        ),
        Number(
            "found_share",
            "Share of DataSF calls found",
            share,
            "percent",
            (
                f"The calls found, of the {len(records)} DataSF calls of the sample. "
                if sampled
                else "The calls found, of the DataSF calls Beacon could read. "
            )
            + meaning,
        ),
        Number(
            "merged",
            "DataSF calls taken to be inside another's incident",
            len(matched.merged),
            "calls",
            "DataSF calls with no incident of their own, for which an incident that is "
            "another call's own, of the same medical-or-not kind, and "
            f"names one of their units or lies within {NEAR_M:.0f} m of them has a "
            "radio call in their time. That is taken to mean the call is inside "
            "another call's incident, where Beacon puts a second call at one block "
            "within 45 minutes and a call that names units and no place. Nothing "
            "checks that it is: an incident that was only near by at the time is "
            "counted the same.",
        ),
        Number(
            "missed",
            "DataSF calls with no incident",
            len(matched.missed),
            "calls",
            "DataSF calls for which Beacon has no incident of their own and none "
            "that they are taken to be inside.",
        ),
        *silent,
        Number(
            "split",
            "Further incidents of a call already counted",
            len(matched.split),
            "incidents",
            "Incidents that fit a DataSF call by the rule and were paired with none, "
            "because each call takes one. That is what is left when Beacon makes more "
            "than one incident of one call, as it does when a unit is heard from after "
            "its incident has been quiet for 45 minutes; an incident that merely fits "
            "a call by its time and a unit or its place is counted the same.",
        ),
        Number(
            "extra",
            "Incidents no DataSF call fits",
            len(matched.extra),
            "incidents",
            "Incidents of Beacon's that fit no DataSF call of "
            + ("the sample" if sampled else "this day")
            + (
                # Of generated calls the count is said and not explained: `fits`
                # shows only that no record fits, which a record can bring about
                # by itself (one whose dispatch is more than `score.EARLY`, two
                # minutes, before its call was received fits no incident opened
                # at its dispatch). And every generated
                # call is of a record, so none is of a call DataSF does not list.
                "."
                if generated
                else ". One can be a real call DataSF does not list, or a wrong "
                "incident: the rule cannot tell."
            ),
        ),
        Number(
            "other_kind",
            "Found under another category",
            wrong_kind,
            "calls",
            "Calls found whose incident has another category than DataSF's call type "
            "maps to. A medical call is never matched to anything but a medical response.",
        ),
    ]


def _place_numbers(
    matched: Matched, *, generated: bool, live: bool, unplaced: int | None
) -> list[Number]:
    away = sorted(pair.away_m for pair in matched.found if pair.away_m is not None)
    measured = (
        "The distance between two points: the one the incident a call was paired with "
        "has, and DataSF's for the call, over the pairs that have both."
    )
    dispatch = (
        "A generated dispatch names the listed block nearest DataSF's point when one "
        f"is within {BLOCK_REACH_M:.0f} m, and an arrival or a clearing names no place; "
        "an incident's point is of a block some call grouped into it was heard to name"
    )
    if generated and not live:
        meaning = (
            f"{measured} {dispatch}. So for a pair that is right and whose point is its "
            "own dispatch's, this is how far that block's middle is from the "
            "intersection; nothing checks that a pair is either. It is a check of the "
            "list of blocks and of the pairing, not a location error of Beacon's hearing."
        )
    elif generated:
        meaning = (
            f"{measured} {dispatch}: the block Muse Ember answered with, which "
            "nothing checks the call said. Nothing checks that a pair is right. It is "
            "no location error on real radio."
        )
    else:
        meaning = (
            f"{measured} The incident's point is the middle of the block Beacon heard, "
            "by the list of blocks, and DataSF's is an intersection: neither is finer "
            "than a block, and nothing checks that a pair is right."
        )
    blockless = []
    if generated and unplaced is not None:
        blockless = [
            Number(
                "unplaced",
                "Calls generated with no block",
                unplaced,
                "calls",
                "DataSF calls for which neither street of the intersection has a "
                f"listed block within {BLOCK_REACH_M:.0f} m of DataSF's point, so the "
                "dispatch generated for them names units and no place.",
            )
        ]
    return [
        *blockless,
        Number(
            "placed",
            "Found calls with a place to compare",
            len(away),
            "calls",
            "Pairs in which the incident has a point and the DataSF record has one.",
        ),
        Number(
            "place_median_m",
            "Distance to DataSF's point, median",
            _percentile(away, 50),
            "m",
            meaning,
        ),
        Number(
            "place_p90_m",
            "Distance to DataSF's point, 90th percentile",
            _percentile(away, 90),
            "m",
            meaning,
        ),
    ]


def _time_numbers(matched: Matched, *, generated: bool) -> list[Number]:
    if generated:
        return [
            Number(
                "heard_after_dispatch_s",
                "From dispatch to the first call heard",
                None,
                "s",
                "Not measured: each generated call is placed at the time DataSF gives, "
                "so how soon the radio tells of a call is not something this run can show.",
            )
        ]
    gaps = sorted(
        (heard - pair.record.dispatched_at).total_seconds()
        for pair in matched.found
        if (heard := first_heard(pair.incident)) is not None
    )
    return [
        Number(
            "heard_after_dispatch_s",
            "From dispatch to the first call heard, median",
            _percentile(gaps, 50),
            "s",
            "From the time DataSF says a call was dispatched to the start of the first "
            "recorded call of the incident Beacon has for it. Negative when the radio "
            "was first. It is the radio's timing, not how long Beacon took to answer.",
        )
    ]


def _cost_numbers(
    spend: ScopeSpend | None, *, live: bool, generated: bool, lost: bool
) -> list[Number]:
    if live and (lost or spend is None):
        unknown = (
            "Not known: the Ember ledger was replaced while the run was under way, so "
            "what the file at its path holds is not what hearing cost. Meta's Usage "
            "page has what was billed."
        )
        return [
            Number("radio_cost_usd", "Cost of hearing", None, "USD", unknown),
            Number("radio_cost_per_1000_usd", "Cost per 1,000 transmissions", None, "USD", unknown),
        ]
    if not live or spend is None:
        nothing = (
            "Not measured: a stand-in heard the calls, nothing was paid, and no cost "
            "of Muse Ember's is known from this run."
        )
        return [
            Number("radio_cost_usd", "Cost of hearing", None, "USD", nothing),
            Number("radio_cost_per_1000_usd", "Cost per 1,000 transmissions", None, "USD", nothing),
        ]
    what = "generated speech, not radio" if generated else "recorded radio"
    scope = SPEECH_SCOPE if generated else RADIO_SCOPE
    over = (
        f"What the Ember ledger holds under the scope {scope}, which is every paid "
        f"run's hearing of {what}, this run's and any other's. A call that was not "
        "answered is counted at the most it can have cost."
    )
    # Each of the three says of itself that it is its scope's, over every paid run:
    # a second paid run of the same kind of audio adds to all of them.
    every = f"every paid run's hearing of {what}, this run's and any other's"
    each = None if not spend.calls else round(to_usd(spend.spent_nanousd) / spend.calls * 1000, 4)
    return [
        Number(
            "radio_cost_usd", "Cost of hearing", round(to_usd(spend.spent_nanousd), 6), "USD", over
        ),
        Number(
            "radio_paid_calls",
            "Paid requests",
            spend.calls,
            "requests",
            f"Requests to Muse Ember that the Ember ledger holds under the scope {scope}, "
            f"over {every}, and not this run's alone. {spend.unanswered} of them were "
            "not answered.",
        ),
        Number(
            "radio_cost_per_1000_usd",
            "Cost per 1,000 transmissions",
            each,
            "USD",
            f"The cost of hearing above for each 1,000 of those requests: a rate over "
            f"{every} under the scope {scope}, and not this run's alone.",
        ),
    ]


def _answered(run: Run) -> bool:
    """Whether an investigation ran to an answer that was the form: it was not cut
    short, Ember was asked, and what it said last could be read."""
    return run.cut_short is None and run.outcome.reason not in ("no_answer", "not_investigated")


def _tap_numbers(
    taps: Sequence[tuple[Replay, Standing, Run | None]],
    reading: Reading,
    matched: Matched,
    *,
    live: bool,
    generated: bool,
    sampled: bool = False,
) -> list[Number]:
    if not taps:
        return []
    who = "Muse Ember" if live else "a stand-in for Ember, by rule"
    runs = [(tap, run) for tap, _, run in taps if run is not None]
    lost = sum(state == "claimed" for _, state, _ in taps)
    # Only an investigation that ran to an answer says anything about answers: one
    # that was cut short is kept as "unconfirmed" and is no answer of anybody's.
    answered = [(tap, run) for tap, run in runs if _answered(run)]
    near = [(tap, run) for tap, run in answered if tap.near is not None]
    away = [(tap, run) for tap, run in answered if tap.near is None]
    right = 0
    for tap, run in near:
        assert tap.near is not None
        wanted = matched.incident_of(tap.near)
        given = reading.kept_as.get(run.outcome.incident_id or "")
        right += run.outcome.status == "incident" and wanted is not None and given == wanted
    unconfirmed = sum(run.outcome.status == "unconfirmed" for _, run in away)
    on = "generated calls" if generated else "recorded radio"
    numbers = [
        Number(
            "taps",
            "Replayed taps answered",
            len(answered),
            "taps",
            f"Of {len(taps)} planned: those whose investigation ran to an answer that "
            "was the form. A replayed tap is a place and a moment of the day put to "
            f"the investigator by a script, investigated by {who} against the run's "
            "evidence as it stood then. No person tapped, and none is stored as a tap.",
        ),
        Number(
            "taps_unanswered",
            "Replayed taps with no answer",
            len(runs) - len(answered),
            "taps",
            "Taps whose investigation was cut short, was refused before anything was "
            "asked, or ended in an answer that was not the form. They are kept as "
            '"unconfirmed", are in none of the counts of answers below, and are not '
            "investigated again.",
        ),
        Number(
            "taps_lost",
            "Replayed taps claimed with no run kept",
            lost,
            "taps",
            "Taps that were claimed and have no run: the process ended between the "
            "claim and the run. They are not investigated again, and what was paid "
            "for them, if anything, is not known from the run's files.",
        ),
        Number(
            "taps_near_matched",
            "Taps at a call, answered with its incident",
            right,
            "taps",
            f"Of {len(near)} answered taps placed at a DataSF call's point three "
            "minutes after its dispatch: those the investigator matched to the "
            "incident that call was paired with, or the one it is taken to be "
            f"inside, on {on}. A tap that was answered "
            '"unconfirmed" is not counted, whether or not that was the careful answer.',
        ),
        Number(
            "taps_away_unconfirmed",
            "Taps away from every call, answered unconfirmed",
            unconfirmed,
            "taps",
            f"Of {len(away)} answered taps placed at a place and a moment such that, "
            + (
                "for every call of the sample within a kilometre, "
                if sampled
                else "for every call DataSF lists within a kilometre, "
            )
            + "the moment is more than "
            "an hour before the call was received or more than an hour after the last "
            "time DataSF records for it (the latest, over all its units, of a "
            "dispatch, a unit on scene, and a unit available again): those answered "
            '"unconfirmed". A call can have gone on '
            "past the last time recorded for it, and a call "
            + (
                "that is not in the sample, of which the run holds nothing, or that "
                "DataSF does not list"
                if sampled
                else "DataSF does not list"
            )
            + " is not known of.",
        ),
    ]
    if not live:
        nothing = (
            "Not measured: a stand-in investigated, nothing was paid, and no cost or "
            "speed of Muse Ember's is known from this run."
        )
        return [
            *numbers,
            Number("tap_cost_usd", "Cost per investigation", None, "USD", nothing),
            Number(
                "tap_seconds_p95", "Time per investigation, 95th percentile", None, "s", nothing
            ),
        ]

    def cost(run: Run) -> float:
        return to_usd(sum(call.cost_nanousd for call in run.calls))

    each = sorted(cost(run) for _, run in answered)
    seconds = sorted(sum(call.latency_ms for call in run.calls) / 1000 for _, run in answered)
    requests = sorted(len(run.calls) for _, run in answered)
    unaccounted = sum(run.unaccounted for _, run in runs)
    missing = (
        f"{unaccounted} requests ended in an error after they may have been sent, and "
        f"{lost} taps were claimed with no run kept: what those cost is not in this "
        "sum and is not known from the run's files."
    )
    return [
        *numbers,
        Number(
            "tap_cost_usd",
            "Cost per answered investigation, mean",
            _mean(each, 6),
            "USD",
            "Over the requests Ember answered in each investigation that ran to an "
            f"answer, paid under {TAPS_SCOPE}. An investigation is held to "
            f"${TAP_BUDGET_USD:.2f}.",
        ),
        Number(
            "tap_cost_total_usd",
            "Cost of the replayed taps that is accounted for",
            round(sum(cost(run) for _, run in runs), 6),
            "USD",
            "Over the requests Ember answered in every investigation that was kept, "
            f"answered or not, paid under {TAPS_SCOPE}. {missing}",
        ),
        Number(
            "tap_requests",
            "Requests per answered investigation, mean",
            _mean([float(count) for count in requests], 2),
            "requests",
            "Requests to Muse Ember that answered, for each investigation that ran to an answer.",
        ),
        Number(
            "tap_seconds_p50",
            "Time per answered investigation, median",
            _percentile(seconds, 50),
            "s",
            "The time Ember took over the requests of an investigation that ran to an "
            "answer, added up. It leaves out Beacon's own tools and the network "
            "between a phone and the server.",
        ),
        Number(
            "tap_seconds_p95",
            "Time per answered investigation, 95th percentile",
            _percentile(seconds, 95),
            "s",
            "The same, for the slowest twentieth.",
        ),
    ]


def _percentile(ordered: Sequence[float], percent: int) -> float | None:
    """The value at a percentile of a sorted list, by nearest rank, or None for an
    empty one."""
    if not ordered:
        return None
    rank = max(1, -(-len(ordered) * percent // 100))
    return round(ordered[min(rank, len(ordered)) - 1], 1)


def _mean(values: Sequence[float], places: int) -> float | None:
    return None if not values else round(sum(values) / len(values), places)


# What one of a counted thing is called.
_ONE = {
    "calls": "call",
    "rows": "row",
    "incidents": "incident",
    "taps": "tap",
    "codes": "code",
    "entries": "entry",
}


def _shown(value: float | int, unit: str) -> str:
    if unit == "USD":
        return f"${value:.4f}"
    if unit == "percent":
        return f"{value}%"
    if value == 1 and unit in _ONE:
        return f"1 {_ONE[unit]}"
    return f"{value:,} {unit}"
