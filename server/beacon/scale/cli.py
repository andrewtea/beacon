"""The scale run's command.

    uv run --project server python scripts/scale_run.py --name NAME --day YYYY-MM-DD

With nothing else, the day's calls are generated as tones from DataSF's records and
a stand-in hears them: no key, no cost, and results that say they are a stand-in's.
`--voice speech --paid` has Muse Ember hear generated speech, and `--recordings DIR
--system NAME --talkgroups LIST --paid` has it hear recordings of real radio, which
also needs `BEACON_RECORDINGS_PERMITTED=1`. A paid run needs `REPLAY=0`,
`MODEL_API_KEY`, and `EMBER_SPENT_BEFORE_USD`.

**The same command, given again, goes on with the run.** What was fetched is not
fetched again, a call that was claimed is not heard again, and a replayed tap that
was claimed is not investigated again. So a run that stopped at a spending cap, or
was interrupted, is finished by running it again; nothing is paid for twice.

Taps are replayed only once the hearing is complete: a tap that is investigated
against half a day's evidence is claimed for good with the answer it got.

One invocation is made at a time under a data directory (`run.alone`), and a paid
run goes on only in the Ember ledger it began to pay in (`run.pin_ledger`). A hold
after a refusal or a failure is the whole run's: while the replay's is on, nothing
is heard either. When the replay of taps first begins the hearing is closed for
good (`run.close_hearing`), so every tap of a run is answered against one evidence.
"""

import argparse
import contextlib
import math
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TextIO

from beacon.ember import EmberClient, EmberError, Storage
from beacon.ember.client import default_data_dir
from beacon.ember.pricing import parse_usd
from beacon.glossary import stand_in_learner
from beacon.ingest.http import SourceError, SourceHttp
from beacon.ingest.recordings import SOURCE as RADIO_SOURCE
from beacon.investigate import Runs, RunsError, stand_in_investigator
from beacon.scale import datasf, generate, glossary, results, score, taps
from beacon.scale.glossary import GLOSSARY_BUDGET_NANOUSD, GLOSSARY_SCOPE
from beacon.scale.run import (
    LEDGER_REPLACED,
    RADIO_BUDGET_NANOUSD,
    TAPS_BUDGET_NANOUSD,
    TAPS_SCOPE,
    Heard,
    Layout,
    PaidClient,
    ScaleError,
    Settings,
    alone,
    begin,
    check_budget,
    close_hearing,
    closed_hearing,
    day_window,
    hear,
    hearing_scope,
    hearing_spend,
    held_for,
    lose_ledger,
    open_store,
    paid_ember,
    pin_ledger,
    prepare,
    radio_ember,
    read_day,
    read_day_calls,
    read_lists,
)

PERMITTED_VARIABLE = "BEACON_RECORDINGS_PERMITTED"


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    http: SourceHttp | None = None,
    paid: PaidClient | None = None,
) -> int:
    """The command. Returns its exit status: 0 for a run that is complete, 1 for
    one that stopped or has calls still unheard, and 2 for one that could not be
    made as it was asked for. `http` is how DataSF is asked and `paid` how a paid
    Ember client is made, each of which a test replaces."""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    options = _parser().parse_args(argv)
    own_http = http is None
    http = SourceHttp() if http is None else http
    problem: str | None = None
    try:
        return _run(options, env, out, http, paid)
    except ScaleError as error:
        problem = str(error)
    except SourceError as error:
        # Its text is a fixed sentence about DataSF, with nothing of a reply.
        problem = str(error)
    except (EmberError, RunsError) as error:
        # A setting that cannot be read, or a file of another mode's: fixed sentences.
        problem = str(error)
    finally:
        if own_http:
            http.close()
    print(f"The scale run was not made. {problem}", file=out)
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scale_run.py",
        description="Run a day of dispatch calls through Beacon and score it against DataSF.",
    )
    parser.add_argument("--name", required=True, help="the run's name, and its directory's")
    parser.add_argument("--day", required=True, help="the day, San Francisco's, as YYYY-MM-DD")
    parser.add_argument(
        "--voice",
        choices=("tones", "speech"),
        default="tones",
        help="what generated calls are: tones only a stand-in hears (default), or "
        "synthesized speech",
    )
    parser.add_argument(
        "--paid",
        action="store_true",
        help="have Muse Ember hear the calls and investigate the taps, under the "
        "scopes scale:radio (recordings) or scale:speech (generated speech), and scale:taps",
    )
    parser.add_argument(
        "--recordings",
        type=Path,
        help="a directory of recorded calls, as trunk-recorder leaves them, that you "
        f"have the right to use ({PERMITTED_VARIABLE}=1); needs --paid",
    )
    parser.add_argument("--system", help="the recorder's short name for the system to hear")
    parser.add_argument("--talkgroups", default="", help="the talkgroups to hear, as 1201,1203")
    parser.add_argument(
        "--ems-talkgroups", default="", help="the ones among them that carry EMS only"
    )
    parser.add_argument(
        "--calls",
        type=int,
        help="make the run of a sample: generate, hear, and score only the first this "
        "many calls of the day, in the order they were dispatched; for generated calls "
        "only, and for good, like everything else a run is",
    )
    parser.add_argument(
        "--taps", type=int, default=taps.MAX_TAPS, help="how many taps to replay; 0 for none"
    )
    parser.add_argument(
        "--radio-budget",
        default="1.25",
        help="the most hearing may cost over every paid run, of recordings and of "
        "generated speech together, in dollars; at most 1.25",
    )
    parser.add_argument(
        "--taps-budget",
        default="0.50",
        help="the most replayed taps may cost over every paid run, in dollars; at most 0.50",
    )
    parser.add_argument(
        "--glossary-budget",
        default="0.25",
        help="the most learning glossaries may cost over every paid run, in dollars; at most 0.25",
    )
    return parser


def _run(
    options: argparse.Namespace,
    env: Mapping[str, str],
    out: TextIO,
    http: SourceHttp,
    paid: PaidClient | None,
) -> int:
    settings = _settings(options, env)
    try:
        radio_budget = parse_usd(options.radio_budget)
        taps_budget = parse_usd(options.taps_budget)
        glossary_budget = parse_usd(options.glossary_budget)
    except ValueError:
        raise ScaleError("A budget is a dollar amount, such as 1.25.") from None
    if not 0 <= options.taps <= taps.MAX_TAPS:
        raise ScaleError(f"From 0 to {taps.MAX_TAPS} taps are replayed.")
    live = settings.hearer == "live"
    if live:
        # Both caps, before anything is fetched, heard, or paid for.
        check_budget(hearing_scope(settings), radio_budget, RADIO_BUDGET_NANOUSD)
        check_budget(TAPS_SCOPE, taps_budget, TAPS_BUDGET_NANOUSD)
        check_budget(GLOSSARY_SCOPE, glossary_budget, GLOSSARY_BUDGET_NANOUSD)
    layout = Layout.under(env, options.name)
    with alone(env), contextlib.ExitStack() as opened:
        radio: EmberClient | None = None
        tap_ember: EmberClient | None = None
        learner: EmberClient | None = None
        # What the run is, first: a run that was begun as another is refused before
        # a paying client is made for it.
        settings = begin(layout, settings)
        # Once the replay of taps has begun, nothing more is heard in the run: it
        # then makes no client to hear with, and needs none of the hearing's budget.
        closed = closed_hearing(layout)
        if live:
            # Every paying client is made now, on the ledger file as it is before
            # anything is heard: a client refuses to pay once the file it was made on
            # is replaced, so a ledger that is replaced between the hearing and the
            # taps is not taken for one in which nothing was spent. The taps' is
            # first, and the ledger is pinned with it; what the other kind of
            # hearing spent is then read from that file, and the hearing's client
            # has to be on the same one (`radio_ember`).
            tap_ember = opened.enter_context(
                contextlib.closing(
                    paid_ember(env, TAPS_SCOPE, taps_budget, TAPS_BUDGET_NANOUSD, paid)
                )
            )
            pin_ledger(layout, tap_ember)
            if closed is None:
                radio = opened.enter_context(
                    contextlib.closing(
                        radio_ember(layout, settings, env, radio_budget, paid, beside=tap_ember)
                    )
                )
            learner = opened.enter_context(
                contextlib.closing(
                    paid_ember(
                        env,
                        GLOSSARY_SCOPE,
                        glossary_budget,
                        GLOSSARY_BUDGET_NANOUSD,
                        paid,
                        beside=tap_ember,
                    )
                )
            )
        if settings.sample is None:
            print(results.basis(settings), file=out)
        prepare(layout, settings, http)
        records, unread = read_day(layout)
        day_calls = read_day_calls(layout)
        if settings.sample is not None:
            # What a sample is of is known once DataSF's record of the day is read.
            print(results.basis(settings, day_calls=day_calls), file=out)
        print(
            f"{day_calls} calls in DataSF's record of {settings.day} that Beacon could read."
            + (
                ""
                if settings.sample is None
                else f" This run is of the first {len(records)} of them."
            ),
            file=out,
        )
        known_blocks = read_lists(layout)[1]
        # DataSF's unit types for the day, which the glossary is compared with and
        # its learner is never shown: fetched before anything is heard or paid for.
        truth = glossary.truth_of(glossary.prepare_types(layout, settings, http))
        if tap_ember is None or learner is None:
            # The stand-ins: one reads the generated calls' script, one investigates
            # by rule, and one guesses a glossary by a rule of thumb.
            learner = opened.enter_context(
                contextlib.closing(
                    EmberClient.fake(Storage.under(default_data_dir(env)), stand_in_learner)
                )
            )
            if closed is None:
                radio = opened.enter_context(
                    contextlib.closing(radio_ember(layout, settings, env, radio_budget, None))
                )
            tap_ember = opened.enter_context(
                contextlib.closing(
                    EmberClient.fake(Storage.under(default_data_dir(env)), stand_in_investigator)
                )
            )
        tap_runs = opened.enter_context(contextlib.closing(Runs(layout.taps, settings.mode)))
        # What the glossary's call did to the run's hold, when an earlier
        # invocation wrote it down and did not get it into the file: before the
        # hold is read, so that nothing is heard or replayed on an older one.
        glossary.settle(layout, tap_runs)
        held = taps.held_for(tap_runs)
        replays = taps.plan(records, options.taps)
        if held is not None:
            # A refusal or a failure on a tap or on the glossary holds the whole run:
            # nothing is heard either, and what the run came to so far is left as it
            # is. A run that is complete, asked for with the plan of taps it was
            # made with, has nothing left to ask: its result is shown as it was
            # written, and not made again.
            before = results.read(layout)
            if (
                before is not None
                and before["complete"]
                and before.get("taps_plan") == taps.plan_name(replays)
            ):
                print("", file=out)
                for line in results.lines(before):
                    print(line, file=out)
                print("", file=out)
                return 0
            print(
                f"The run is leaving {results.who(live=live)} alone after a refusal or "
                "a failure in the replay of taps or in learning the glossary. "
                f"{_hold_left(held)} Nothing was "
                "asked, and the run's results are as they were. Give the same command "
                "again after that.",
                file=out,
            )
            return 1
        store = open_store(layout, settings)
        opened.callback(store.close)
        if radio is not None:
            heard = hear(layout, settings, store, radio, env, recordings=options.recordings)
        else:
            # The hearing is closed, so nothing more is heard in this run. What
            # its hearing cost is read from the ledger the taps' client is on, which
            # is the one the run is pinned to.
            assert closed is not None
            spend, lost = hearing_spend(settings, tap_ember) if live else (None, False)
            heard = Heard(last_pass=closed, stopped=None, spend=spend, ledger_lost=lost)
        heard_complete = (
            heard.stopped is None and heard.last_pass is not None and heard.last_pass.unready == 0
        )
        ledger_lost = heard.ledger_lost
        taps_stopped: taps.Stopped | None = None
        if heard_complete and not ledger_lost and closed is None:
            # Before the first tap is claimed and the glossary is asked for: from
            # here on everything made of the whole day is of the evidence as it is now.
            assert heard.last_pass is not None
            close_hearing(layout, heard.last_pass)
            closed = heard.last_pass
        if replays and heard_complete and not ledger_lost:
            taps_stopped = taps.replay(replays, tap_ember, store, known_blocks, tap_runs).stopped
        elif replays:
            print("No tap is replayed until every call has been heard.", file=out)
        standing = taps.kept(replays, tap_runs) if heard_complete else []
        if live and (
            taps_stopped == "ledger_replaced"
            or (radio is not None and radio.ledger_replaced())
            or tap_ember.ledger_replaced()
        ):
            # Asked of both clients when everything is done, whether or not either
            # had a request left to make: the file at the ledger's path is another.
            ledger_lost = True
        reading = score.read_incidents(store, known_blocks)
        learned: glossary.Learned | None = None
        if heard_complete and not ledger_lost and taps_stopped != "store":
            # After the taps, which a failure here does not then hold up; while a
            # hold of theirs is on, nothing is asked for the glossary either. When
            # the investigations file could not be written, a hold the replay put
            # on may not be in it: nothing is asked for a glossary then.
            learned = glossary.learn(layout, reading.incidents, learner, tap_runs)
            if learned.ledger_lost or (live and learner.ledger_replaced()):
                ledger_lost = True
        if ledger_lost:
            # Kept, so that the command given again is refused and pays nothing.
            lose_ledger(layout)
        script = generate.read_script(layout.calls) if settings.audio == "generated" else None
        made = results.build(
            options.name,
            settings,
            records,
            unread=unread,
            day_calls=day_calls,
            rows=store.count_by_source().get(RADIO_SOURCE, 0),
            no_row=store.claimed_without_row(RADIO_SOURCE),
            listed=None if script is None else script.calls,
            unplaced=None if script is None else script.unplaced,
            last_pass=heard.last_pass,
            stopped=heard.stopped,
            closed=closed is not None,
            reading=reading,
            matched=score.match(records, reading.incidents),
            radio_spend=None if ledger_lost else heard.spend,
            ledger_lost=ledger_lost,
            taps=standing,
            taps_stopped=taps_stopped,
            taps_plan=taps.plan_name(replays),
            glossary=(
                []
                if learned is None
                else glossary.numbers(
                    learned, truth, generated=settings.audio == "generated", live=live
                )
            ),
            glossary_waiting=(
                None if learned is None else glossary.waiting_text(learned, live=live)
            ),
        )
        # Published under the lock: a later invocation's results are not written
        # over by an earlier one's.
        results.write(layout, made)
        # A hold that is on now, in the hearing or in the replay, as it stands at
        # this moment: the results, which are kept, name no length for one.
        hold_left = max(held_for(store) or 0.0, taps.held_for(tap_runs) or 0.0)
    print("", file=out)
    for line in results.lines(made):
        print(line, file=out)
    print("", file=out)
    if ledger_lost:
        print(LEDGER_REPLACED, file=out)
        return 1
    if not made["complete"]:
        if hold_left > 0:
            print(
                f"The run is not finished, and a hold is on. {_hold_left(hold_left)} "
                "Give the same command again after that to go on with it.",
                file=out,
            )
        else:
            print(
                "The run is not finished. Give the same command again to go on with it.",
                file=out,
            )
        return 1
    return 0


def _hold_left(seconds: float) -> str:
    """How long a hold still has to run, as it was read just now."""
    minutes = math.ceil(seconds / 60)
    return f"The hold has about {minutes} more minute{'' if minutes == 1 else 's'} to run."


def _settings(options: argparse.Namespace, env: Mapping[str, str]) -> Settings:
    """What the run that is asked for is. Raises ScaleError for what is no run."""
    try:
        day = datetime.strptime(options.day, "%Y-%m-%d").date()
    except ValueError:
        raise ScaleError("--day is a day, as YYYY-MM-DD.") from None
    if day >= datetime.now(datasf.LOCAL).date():
        raise ScaleError("--day is a day that is over: DataSF's record of it is what is scored.")
    if options.calls is not None and options.recordings is not None:
        raise ScaleError(
            "A sample is of generated calls: recordings are heard as they were made, "
            "and --calls does not go with --recordings."
        )
    if options.calls is not None and options.calls < 1:
        raise ScaleError("--calls is how many calls a sample holds: 1 or more.")
    if options.recordings is None:
        if options.paid and options.voice == "tones":
            raise ScaleError(
                "A tone says nothing, and Muse Ember is not paid to hear one: a paid "
                "run of generated calls is --voice speech --paid."
            )
        return Settings(
            day=day.isoformat(),
            audio="generated",
            voice=options.voice,
            hearer="live" if options.paid else "fake",
            sample=options.calls,
        )
    if not options.paid:
        raise ScaleError(
            "Recorded radio is heard by Muse Ember only, never by a stand-in, whose "
            "answer would be an invention about a real call: --recordings needs --paid."
        )
    if env.get(PERMITTED_VARIABLE) != "1":
        raise ScaleError(
            f"A recording of radio is used only with the right to: set {PERMITTED_VARIABLE}=1 "
            "when the recordings are your own, or you have the permission of whoever "
            "made them. Beacon does not use OpenMHz's recordings, and has no license "
            "from Broadcastify."
        )
    if datetime.now(datasf.UTC) < day_window(day)[1]:
        # Before anything of the day is fetched or kept: DataSF's record of a day
        # that is hardly over is not whole either.
        raise ScaleError(
            "The day's last calls can still be recorded: a run over recordings is made "
            "from two hours after the end of San Francisco's day."
        )
    talkgroups = _numbers(options.talkgroups)
    ems = _numbers(options.ems_talkgroups)
    if not options.system or not talkgroups or not set(ems) <= set(talkgroups):
        raise ScaleError(
            "Recordings need --system, the recorder's short name for the one system to "
            "hear, and --talkgroups, that system's numbers for the channels to hear; "
            "--ems-talkgroups names the ones among them that carry EMS only."
        )
    return Settings(
        day=day.isoformat(),
        audio="recorded",
        voice=None,
        hearer="live",
        system=options.system,
        talkgroups=talkgroups,
        ems_talkgroups=ems,
    )


def _numbers(said: str) -> tuple[int, ...]:
    try:
        return tuple(sorted({int(part) for part in said.split(",") if part.strip()}))
    except ValueError:
        raise ScaleError("Talkgroups are a list of numbers, such as 1201,1203.") from None
