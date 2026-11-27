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
  are scored against, so a match says Ember made out synthetic speech of those
  records, not that Beacon finds a real city's calls.
- **recorded radio, heard by Muse Ember**: the one kind whose numbers are about
  Beacon on real radio, scored against DataSF's record of the same day.

What is not measured in any run: whether what an incident says is right beyond its
kind and its place, and anything about calls DataSF does not list.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
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

GENERATED_BY_STAND_IN = (
    "Stand-in run on generated calls. The calls were generated from DataSF's own "
    'records of this day and "heard" by a stand-in that looks each call up in the '
    "generator's script. No radio was heard and Muse Ember was asked nothing. "
    "Nothing below measures Beacon's accuracy, speed, or cost on real radio: it shows "
    "only that the pipeline keeps what it is given."
)
GENERATED_BY_EMBER = (
    "Generated speech, heard by Muse Ember. The calls are synthesized speech "
    "generated from DataSF's own records of this day, and Muse Ember was paid to hear "
    "them. They are scored against the records they were generated from, so a match "
    "says Ember made out clean synthetic speech of those records. It does not measure "
    "Beacon on real radio, which is noisy, clipped, and says things no record holds."
)
RECORDED_BY_EMBER = (
    "Recorded radio, heard by Muse Ember. The calls are recordings of dispatch radio "
    "that the run's operator said they have the right to use, heard by Muse Ember and "
    "scored against DataSF's record of the same day."
)
NOT_CHECKED = (
    "A match is by kind, time, and unit or place. Nothing checks what an incident "
    "says beyond that, and a call DataSF does not list cannot be told from a wrong "
    "incident."
)
INCOMPLETE = (
    "The run is not complete: calls are still unheard or were not ready. The numbers "
    "below are of what was heard so far."
)
PARTIAL_READING = "The incident engine left rows out of a reading, so an incident can lack calls."

_STOPPED = {
    "budget": "The run stopped at its spending cap or the spending stop.",
    "held": (
        "The run stopped because it is leaving Ember alone for ten minutes after a "
        "refusal or a failure. Give the same command again after that."
    ),
    "ledger_replaced": (
        "The run stopped because the Ember ledger was replaced under it. Compare "
        "Meta's Usage page with the ledger before anything more is paid for."
    ),
    "ember": "The run stopped because a call to Ember failed.",
    "source": "The run stopped because the directory of calls could not be read.",
    "store": "The run stopped because its evidence store could not be written.",
}


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

    def as_data(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "text": self.text,
        }


def basis(settings: Settings) -> str:
    """What a run is, and what follows for its numbers."""
    if settings.audio == "recorded":
        return RECORDED_BY_EMBER
    return GENERATED_BY_EMBER if settings.hearer == "live" else GENERATED_BY_STAND_IN


def build(
    name: str,
    settings: Settings,
    records: Sequence[datasf.Record],
    *,
    rows: int,
    no_row: int,
    listed: int | None,
    unplaced: int | None = None,
    last_pass: PassCount | None,
    stopped: str | None,
    reading: Reading,
    matched: Matched,
    radio_spend: ScopeSpend | None,
    taps: Sequence[tuple[Replay, Standing, Run | None]],
    taps_stopped: str | None,
    made_at: datetime | None = None,
) -> dict[str, Any]:
    """A run's results, as they are written and shown. `rows` is how many radio
    rows the run's store holds, `no_row` how many calls are claimed there with no
    row, and `listed` how many calls there are to hear, when that is known;
    `unplaced` is how many records the generator found no block for;
    `last_pass` and `stopped` are how the latest hearing ended."""
    live = settings.hearer == "live"
    generated = settings.audio == "generated"
    complete = stopped is None and last_pass is not None and last_pass.unready == 0
    caveats = [NOT_CHECKED]
    if not complete:
        caveats.insert(0, INCOMPLETE)
    if stopped is not None:
        caveats.insert(1, _STOPPED.get(stopped, "The run stopped early."))
    if reading.partial:
        caveats.append(PARTIAL_READING)
    numbers = [
        *_record_numbers(records),
        *_hearing_numbers(rows, no_row, listed, last_pass, live=live),
        *_match_numbers(records, reading, matched, generated=generated, live=live),
        *_place_numbers(matched, generated=generated, unplaced=unplaced),
        *_time_numbers(matched, generated=generated),
        *_cost_numbers(radio_spend, live=live, generated=generated),
        *_tap_numbers(taps, reading, matched, live=live, generated=generated),
    ]
    if taps_stopped is not None:
        caveats.append(
            "The replay of taps stopped early: Ember refused or failed, or the "
            "investigations file could not be written."
        )
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
        "complete": complete,
        "basis_text": basis(settings),
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
    for number in results["numbers"]:
        value = "none" if number["value"] is None else _shown(number["value"], number["unit"])
        out.append(f"{number['label']}: {value}")
        out.append(f"    {number['text']}")
    return out


def _record_numbers(records: Sequence[datasf.Record]) -> list[Number]:
    medical = sum(record.medical for record in records)
    return [
        Number(
            "records",
            "Calls DataSF lists",
            len(records),
            "calls",
            f"Calls the fire department was dispatched to on this day, by DataSF's record. "
            f"{medical} of them are medical, of which Beacon keeps the crews, the "
            "intersection, and the times, and nothing else.",
        )
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
) -> list[Number]:
    if generated and not live:
        meaning = (
            "The calls were generated from these records and a stand-in read them "
            "back, so this is a check of the pipeline, not a measure of accuracy."
        )
    elif generated:
        meaning = (
            "The calls were generated from these records, so this measures Ember's "
            "hearing of clean synthetic speech and the pipeline, not of real radio."
        )
    else:
        meaning = "This is Beacon on recorded radio, by the matching rule stated above."
    share = None if not records else round(100 * len(matched.found) / len(records), 1)
    silent = []
    if generated:
        unheard = {record.number for record in records if not record.units}
        by_chance = sum(record.number in unheard for record in matched.merged)
        silent = [
            Number(
                "not_generated",
                "DataSF calls no call was generated for",
                len(unheard),
                "calls",
                "DataSF calls that name no unit whose id is shaped as Beacon keeps one "
                "(a type code and a number), so the generator made no call for them "
                f"and none can have been found. {by_chance} of them are counted above "
                "as taken to be inside another's incident, which for a call nobody "
                "generated is chance: some incident was near it at the time. That is "
                "how loose that count is.",
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
            "DataSF calls for which Beacon has an incident of its own: of the same "
            "medical-or-not kind, opened within the call's time, and naming one of its "
            f"units or placed within {NEAR_M:.0f} m of it. {meaning}",
        ),
        Number(
            "found_share",
            "Share of DataSF calls found",
            share,
            "percent",
            f"The calls found, of the calls DataSF lists. {meaning}",
        ),
        Number(
            "merged",
            "DataSF calls taken to be inside another's incident",
            len(matched.merged),
            "calls",
            "DataSF calls with no incident of their own, for which an incident that "
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
            "Incidents that fit a DataSF call and are not that call's own: Beacon made "
            "more than one incident of one call, as it does when a unit is heard from "
            "after its incident has been quiet for 45 minutes. Such an incident often "
            "has no place.",
        ),
        Number(
            "extra",
            "Incidents no DataSF call fits",
            len(matched.extra),
            "incidents",
            "Incidents of Beacon's that fit no DataSF call of this day. One can be a "
            "real call DataSF does not list, or a wrong incident: the rule cannot tell.",
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


def _place_numbers(matched: Matched, *, generated: bool, unplaced: int | None) -> list[Number]:
    away = sorted(pair.away_m for pair in matched.found if pair.away_m is not None)
    if generated:
        meaning = (
            "Each generated call names the listed block nearest DataSF's point, so "
            "this is how far that block's middle is from the intersection: a check of "
            "the list of blocks, not a location error of Beacon's hearing."
        )
    else:
        meaning = (
            "From the middle of the block Beacon heard, by the list of blocks, to the "
            "intersection DataSF gives. Neither is finer than a block."
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
                "generated call names units and no place, and its incident has no "
                "point to compare.",
            )
        ]
    return [
        *blockless,
        Number(
            "placed",
            "Found calls with a place to compare",
            len(away),
            "calls",
            "Calls found whose incident has a point and whose DataSF record has one.",
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


def _cost_numbers(spend: ScopeSpend | None, *, live: bool, generated: bool) -> list[Number]:
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
            f"Requests to Muse Ember under {scope}, {spend.unanswered} of them not answered.",
        ),
        Number(
            "radio_cost_per_1000_usd",
            "Cost per 1,000 transmissions",
            each,
            "USD",
            f"That cost for each 1,000 of those requests, for {what}.",
        ),
    ]


def _tap_numbers(
    taps: Sequence[tuple[Replay, Standing, Run | None]],
    reading: Reading,
    matched: Matched,
    *,
    live: bool,
    generated: bool,
) -> list[Number]:
    if not taps:
        return []
    who = "Muse Ember" if live else "a stand-in for Ember, by rule"
    runs = [(tap, run) for tap, _, run in taps if run is not None]
    near = [(tap, run) for tap, run in runs if tap.near is not None]
    away = [(tap, run) for tap, run in runs if tap.near is None]
    right = 0
    for tap, run in near:
        assert tap.near is not None
        wanted = matched.incident_of(tap.near)
        answered = reading.kept_as.get(run.outcome.incident_id or "")
        right += run.outcome.status == "incident" and wanted is not None and answered == wanted
    unconfirmed = sum(run.outcome.status == "unconfirmed" for _, run in away)
    on = "generated calls" if generated else "recorded radio"
    numbers = [
        Number(
            "taps",
            "Replayed taps investigated",
            len(runs),
            "taps",
            f"Of {len(taps)} planned. A replayed tap is a place and a moment of the day "
            f"put to the investigator by a script, investigated by {who} against the "
            "run's evidence as it stood then. No person tapped, and none is stored as a tap.",
        ),
        Number(
            "taps_near_matched",
            "Taps at a call, answered with its incident",
            right,
            "taps",
            f"Of {len(near)} taps placed at a DataSF call's point three minutes after "
            "its dispatch: those the investigator matched to the incident Beacon has "
            f"for that call, or the one that call is taken to be inside, on {on}. A "
            "tap that was "
            'answered "unconfirmed" is not '
            "counted, whether or not that was the careful answer.",
        ),
        Number(
            "taps_away_unconfirmed",
            "Taps away from every call, answered unconfirmed",
            unconfirmed,
            "taps",
            f"Of {len(away)} taps placed where DataSF lists no call within a kilometre "
            'for an hour either side: those answered "unconfirmed".',
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
    costs = sorted(to_usd(sum(call.cost_nanousd for call in run.calls)) for _, run in runs)
    seconds = sorted(sum(call.latency_ms for call in run.calls) / 1000 for _, run in runs)
    requests = sorted(len(run.calls) for _, run in runs)
    unaccounted = sum(run.unaccounted for _, run in runs)
    counted = (
        f"Over the requests of each investigation that Ember answered, paid under "
        f"{TAPS_SCOPE}. {unaccounted} requests ended in an error after they may have "
        "been sent and are not in these sums. An investigation is held to "
        f"${TAP_BUDGET_USD:.2f}."
    )
    return [
        *numbers,
        Number("tap_cost_usd", "Cost per investigation, mean", _mean(costs, 6), "USD", counted),
        Number(
            "tap_cost_total_usd",
            "Cost of the replayed taps",
            round(sum(costs), 6),
            "USD",
            counted,
        ),
        Number(
            "tap_requests",
            "Requests per investigation, mean",
            _mean([float(count) for count in requests], 2),
            "requests",
            "Requests to Muse Ember that answered, for each investigation.",
        ),
        Number(
            "tap_seconds_p50",
            "Time per investigation, median",
            _percentile(seconds, 50),
            "s",
            "The time Ember took over an investigation's requests, added up. It leaves "
            "out Beacon's own tools and the network between a phone and the server.",
        ),
        Number(
            "tap_seconds_p95",
            "Time per investigation, 95th percentile",
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


def _shown(value: float | int, unit: str) -> str:
    if unit == "USD":
        return f"${value:.4f}"
    if unit == "percent":
        return f"{value}%"
    return f"{value} {unit}"
