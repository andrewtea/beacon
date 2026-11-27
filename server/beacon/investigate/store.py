"""Where investigations are kept: one run for each tap, and the decisions on crisis mode.

A tap's investigation is paid for, and asking again can come to another answer. So a
run is kept here, under its tap's id, in a file of its own: `data/investigate/
mock.sqlite3` or `data/investigate/live.sqlite3`, as the evidence store has one file
for each.

- **A tap is claimed before anything is paid for it** (`claim`), and the claim is for
  good: a tap is investigated once. A claim is never given up, because a run makes
  several paid calls and what is known after a failure is seldom that none of them
  was sent.
- **Nothing a person sent and nothing a source said is written here.** A run
  (`beacon.investigate.loop.Run`) holds ids, counts, tags from closed lists, and
  each Ember call's tokens, time, cost, and ledger row. It holds no sentence: what a
  person is shown is worded from it when it is read (`beacon.investigate.card`), so
  a change of wording changes nothing that is kept. It does not hold where the tap
  came from, which stays in the tap's own evidence row.
- **Crisis mode is turned on for an incident by the first run that decides for it
  and whose decision stands**, and that decision is the one kept, without its
  tap's id: a tap's id is what its sender reads its answer with, and what is shown
  of an incident is shown to everyone. A run that decides against crisis mode
  turns nothing on and nothing off, and is kept only as its tap's run. With the
  decision are kept the signals it rested on as they were then: each one's kind,
  its confidence, and the evidence rows under it.
- **The live file holds nothing a stand-in had a part in.** `write` refuses such a
  run there.
- Rows older than `KEPT` are deleted (`sweep`): whenever a tap is claimed or
  passed over, and by the server every minute, so that they go when no tap comes.
  A claim goes with its row, so whoever pays for a run refuses a tap older than
  the desk looks into (`desk.STALE`), which is far less than `KEPT`: a tap whose
  claim was swept is never claimed again.
- The file's shape is its `user_version` (`_FORMAT`). When what `_run_json` writes
  changes, raise it and add the old number to `_EARLIER`; `beacon.files.begin_file`
  then drops the runs of an earlier shape and keeps their claims.
"""

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from beacon.errors import raise_bare
from beacon.files import Mode, begin_file
from beacon.investigate.loop import Cited, Outcome, Round, Run
from beacon.investigate.tools import Accounting, Call

# How long a run is kept: as long as a story is.
KEPT = timedelta(hours=24)

_WAIT_SECONDS = 5.0
_FORMAT = 3
# The earlier shapes whose runs and decisions are dropped. 0 is a file that says no
# shape, which a new file is too. 1 kept a decision's signals as their kinds only,
# and of `nearby_incidents` only the ids.
_EARLIER = frozenset({0, 1})
# The earlier shapes whose runs and decisions are read as they are. 2 did not keep
# which feeds a count, a ground, or a cited signal rested on (`credits`,
# `aircraft_credits`, `rotorcraft_credits`, and a cited signal's `credits`); a run of
# 2 is read as naming none. The number is raised all the same, so that an older
# Beacon refuses a file whose runs name feeds it would not show the credit of.
_READABLE = frozenset({2})
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS run (
        tap_id TEXT PRIMARY KEY,
        claimed_at TEXT NOT NULL,
        finished_at TEXT,
        run TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crisis (
        incident_id TEXT PRIMARY KEY,
        decided_at TEXT NOT NULL,
        decision TEXT NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_UNKNOWN_SHAPE = (
    "The investigations file is in a shape this Beacon does not know: a newer one "
    "wrote it. It is left as it is. Run a Beacon that knows the shape. Do not delete "
    "the file or move it away to begin another: it is the record of which taps were "
    "paid for, and without it the same taps are paid for again."
)


class RunsError(Exception):
    """The investigations file cannot be used as asked. The text is fixed."""


@dataclass(frozen=True)
class Decision:
    """Ember's decision that an incident's signals add up to crisis mode."""

    incident_id: str
    decided_at: datetime
    # The signals of the incident it rested on, as the engine had them then.
    cited: tuple[Cited, ...]
    # Which kind of Ember client decided, and whether a stand-in had a part in it.
    model: str | None
    stand_in: bool


class Runs:
    """One file of investigations. It is safe to share between threads."""

    def __init__(self, path: Path, mode: Mode) -> None:
        """Raises RunsError when the file was begun as the other mode's, and when
        it is in a shape this Beacon does not know. Such a file is left as it was."""
        self._path = path
        self._mode = mode
        self._closing = threading.Lock()
        self._closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        refusal = begin_file(
            path,
            mode,
            what="investigations",
            schema=_SCHEMA,
            shape=_FORMAT,
            earlier=_EARLIER,
            drop=("UPDATE run SET run = NULL WHERE run IS NOT NULL", "DELETE FROM crisis"),
            unknown=_UNKNOWN_SHAPE,
            readable=_READABLE,
        )
        if refusal is not None:
            raise RunsError(refusal)

    @property
    def mode(self) -> Mode:
        return self._mode

    def close(self) -> None:
        """Write nothing more. A write that is under way is finished first. Reading
        goes on working."""
        with self._closing:
            self._closed = True

    def claim(self, tap_id: str, now: datetime) -> bool:
        """Claim a tap's investigation, and say whether the claim is this caller's:
        False when the tap was claimed before, by anyone, or was passed over.

        Raises RunsError when the file is closed."""
        with self._writing() as db:
            taken = db.execute("SELECT 1 FROM run WHERE tap_id = ?", (tap_id,)).fetchone()
            if taken is not None:
                return False
            _sweep(db, now)
            db.execute("INSERT INTO run (tap_id, claimed_at) VALUES (?, ?)", (tap_id, _stamp(now)))
        return True

    def sweep(self, now: datetime) -> int:
        """Delete the runs and the decisions that are older than `KEPT`, and say
        how many rows went.

        Raises RunsError when the file is closed."""
        with self._writing() as db:
            return _sweep(db, now)

    def write(self, run: Run, *, state: Mapping[str, Any] | None = None) -> None:
        """Keep the run made under a claim, and its decision on crisis mode when it
        made one. `state` is what the caller must remember between runs, saved in
        the same transaction.

        Raises RunsError when the live file is handed a run a stand-in had a part
        in, when the tap is not claimed or its run is already written, and when the
        file is closed."""
        if self._mode == "live" and run.stand_in:
            raise_bare(RunsError("The live investigations file holds nothing a stand-in made."))
        outcome = run.outcome
        with self._writing() as db:
            changed = db.execute(
                "UPDATE run SET finished_at = ?, run = ? WHERE tap_id = ? AND run IS NULL",
                (_stamp(run.finished_at), json.dumps(_run_json(run)), run.tap_id),
            ).rowcount
            if changed != 1:
                raise_bare(RunsError("A run is written only under a claim, once."))
            if outcome.crisis and outcome.incident_id is not None:
                decision = {
                    "cited": [asdict(cited) for cited in outcome.crisis_cited],
                    "model": run.model,
                    "stand_in": run.stand_in,
                }
                # The first decision that turned crisis mode on is the one kept.
                db.execute(
                    "INSERT OR IGNORE INTO crisis (incident_id, decided_at, decision) "
                    "VALUES (?, ?, ?)",
                    (outcome.incident_id, _stamp(run.finished_at), json.dumps(decision)),
                )
            if state is not None:
                _save_state(db, state)

    def skip(self, tap_id: str, now: datetime) -> None:
        """Record that a tap was passed over, and not investigated, when it has no
        row yet. Nothing was paid for it, and nothing will be.

        Raises RunsError when the file is closed."""
        run = Run(
            tap_id=tap_id,
            started_at=now,
            finished_at=now,
            outcome=Outcome(status="unconfirmed", reason="not_investigated"),
        )
        with self._writing() as db:
            _sweep(db, now)
            db.execute(
                "INSERT OR IGNORE INTO run (tap_id, claimed_at, finished_at, run) "
                "VALUES (?, ?, ?, ?)",
                (tap_id, _stamp(now), _stamp(now), json.dumps(_run_json(run))),
            )

    def get(self, tap_id: str) -> tuple[datetime, Run | None] | None:
        """When a tap was claimed and its run, which is None while it is being
        made, and for a run of an earlier shape. None for a tap with no row."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT claimed_at, run FROM run WHERE tap_id = ?", (tap_id,)
            ).fetchone()
        if row is None:
            return None
        claimed = datetime.strptime(row[0], _STAMP).replace(tzinfo=UTC)
        return claimed, None if row[1] is None else _run_from(tap_id, json.loads(row[1]))

    def crisis(self, incident_id: str) -> Decision | None:
        """The decision on crisis mode that was made for an incident, or None."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT decided_at, decision FROM crisis WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            return None
        kept = json.loads(row[1])
        return Decision(
            incident_id=incident_id,
            decided_at=datetime.strptime(row[0], _STAMP).replace(tzinfo=UTC),
            cited=tuple(_cited(cited) for cited in kept["cited"]),
            model=kept["model"],
            stand_in=kept["stand_in"],
        )

    def state(self) -> dict[str, Any]:
        """What `save_state` or `write` saved last, or an empty mapping."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'state'").fetchone()
        return {} if row is None else json.loads(row[0])

    def save_state(self, state: Mapping[str, Any]) -> None:
        """Raises RunsError when the file is closed."""
        with self._writing() as db:
            _save_state(db, state)

    def _connect(self) -> sqlite3.Connection:
        # A connection for each operation, as the evidence store does. The file
        # was accepted when this object was built, and its journal mode set.
        return sqlite3.connect(self._path, timeout=_WAIT_SECONDS, isolation_level=None)

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """One write, begun only while the file is open and committed before
        `close` returns."""
        with self._closing:
            if self._closed:
                raise_bare(RunsError("The investigations file is closed, and takes no more."))
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")


_STAMP = "%Y-%m-%dT%H:%M:%S.%f"


def _stamp(moment: datetime) -> str:
    """A moment as text that sorts as time does: UTC, always with microseconds."""
    return moment.astimezone(UTC).strftime(_STAMP)


def _sweep(db: sqlite3.Connection, now: datetime) -> int:
    old = _stamp(now - KEPT)
    runs = db.execute("DELETE FROM run WHERE claimed_at < ?", (old,)).rowcount
    return runs + db.execute("DELETE FROM crisis WHERE decided_at < ?", (old,)).rowcount


def _cited(kept: Mapping[str, Any]) -> Cited:
    return Cited(
        kind=kept["kind"],
        confidence=kept["confidence"],
        evidence_ids=tuple(kept["evidence_ids"]),
        # A decision of the shape before this one kept none.
        credits=tuple(kept.get("credits", ())),
    )


def _save_state(db: sqlite3.Connection, state: Mapping[str, Any]) -> None:
    db.execute(
        "INSERT INTO meta (key, value) VALUES ('state', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (json.dumps(dict(state)),),
    )


def _run_json(run: Run) -> dict[str, Any]:
    """A run as it is kept. Its fields are ids, counts, tags, times, and numbers."""
    return {
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "outcome": asdict(run.outcome),
        "rounds": [
            {
                "n": round_.n,
                "ember": asdict(round_.ember),
                "calls": [
                    {
                        "tool": call.tool,
                        "thinking": call.thinking,
                        "lead": call.lead,
                        "why": call.why,
                        "args": dict(call.args),
                        "ok": call.ok,
                        "returned": dict(call.returned),
                        "ember": None if call.ember is None else asdict(call.ember),
                    }
                    for call in round_.calls
                ],
            }
            for round_ in run.rounds
        ],
        "final": None if run.final is None else asdict(run.final),
        "model": run.model,
        "stand_in": run.stand_in,
        "cut_short": run.cut_short,
        "as_of": None if run.as_of is None else run.as_of.isoformat(),
        "last_id": run.last_id,
        "unaccounted": run.unaccounted,
    }


def _run_from(tap_id: str, kept: dict[str, Any]) -> Run:
    outcome = kept["outcome"]
    return Run(
        tap_id=tap_id,
        started_at=datetime.fromisoformat(kept["started_at"]),
        finished_at=datetime.fromisoformat(kept["finished_at"]),
        outcome=Outcome(
            **{
                **outcome,
                "grounds": tuple(outcome["grounds"]),
                "crisis_cited": tuple(_cited(cited) for cited in outcome["crisis_cited"]),
            }
        ),
        rounds=tuple(
            Round(
                n=round_["n"],
                ember=_accounting(round_["ember"]),
                calls=tuple(
                    Call(
                        tool=call["tool"],
                        thinking=call["thinking"],
                        lead=call["lead"],
                        why=call["why"],
                        args=call["args"],
                        ok=call["ok"],
                        returned=call["returned"],
                        ember=None if call["ember"] is None else _accounting(call["ember"]),
                    )
                    for call in round_["calls"]
                ),
            )
            for round_ in kept["rounds"]
        ),
        final=None if kept["final"] is None else _accounting(kept["final"]),
        model=kept["model"],
        stand_in=kept["stand_in"],
        cut_short=kept["cut_short"],
        as_of=None if kept["as_of"] is None else datetime.fromisoformat(kept["as_of"]),
        last_id=kept["last_id"],
        unaccounted=kept["unaccounted"],
    )


def _accounting(kept: dict[str, Any]) -> Accounting:
    generation, row, token = kept["ledger"]
    return Accounting(**{**kept, "ledger": (generation, row, token)})
