"""Where stories are kept: beside the incident engine, and not in it.

An incident is derived from the evidence and never stored. What Ember chooses to
tell of one is not derived: it was paid for, and asking again can give another
choice. So a story
is kept here, under the incident's id and the basis it was written from
(`beacon.story.merge.basis_of`), in a file of its own: `data/story/mock.sqlite3` or
`data/story/live.sqlite3`, as the evidence store has one file for each.

- **A basis is claimed before it is paid for** (`claim`), and a claim is one
  caller's, for good: the same lines are never paid for twice. A claim is also
  refused while the incident's last one is younger than the caller's interval, so
  an incident that changes every few seconds is not written about every few seconds.
- **No word a model wrote is written here.** A story holds the sentences code
  wrote for the points Ember chose, the sources code gave them, and the call's
  token counts. The file holds nothing a person sent and nothing a source said
  beyond what an incident's own lines show.
- **The live file holds nothing a stand-in chose.** `write` refuses a story whose
  model is `fake` there.
- Rows older than `KEPT` are deleted when a new one is claimed: the engine reads
  twelve hours, and an incident older than that has no reader.
- **A file of an earlier shape keeps its claims and none of its stories.** What a
  story holds and says is the file's `user_version` (`_FORMAT`); opening a file of
  an earlier one (`_EARLIER`) empties its stories, which are never read, and leaves
  each row as a claim. A sentence is kept as code wrote it, so a change of wording
  is a change of shape. A file of a shape this Beacon does not know, and a file of
  the other mode, are refused, and nothing of them is written (`_begin`).
"""

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from beacon.errors import raise_bare
from beacon.incident import Credit, Source
from beacon.story.merge import Claim, Draft

Mode = Literal["live", "mock"]

# How long a row is kept: twice what the incident engine reads.
KEPT = timedelta(hours=24)

_WAIT_SECONDS = 5.0
# What a kept story holds and says, as the file's `user_version`. A story's sentences
# are kept as code wrote them and shown as they were kept, so raise this whenever
# `_as_json` changes what it writes **and whenever a sentence code writes for a story
# changes** (`beacon.story.merge`, and the engine's timeline lines for a 511 event,
# an aircraft, and taps, which a story reuses), and add the number it had to
# `_EARLIER`: a file
# of an earlier number then has its stories dropped when it is opened, and keeps its
# claims. A file of any other number, which a newer Beacon wrote, is refused as it is.
_FORMAT = 3
# The earlier ones: 0 is a file that says none (the first shape, and a new file),
# and 2 held sentences in words that have since changed.
_EARLIER = frozenset({0, 2})
# One statement each: they are run inside the transaction that begins a file.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS story (
        incident_id TEXT NOT NULL,
        basis TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        written_at TEXT,
        story TEXT,
        PRIMARY KEY (incident_id, basis)
    )
    """,
    "CREATE INDEX IF NOT EXISTS story_latest ON story (incident_id, written_at)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
# The file is the only record of which lines were paid for, so the advice is never
# to begin another one.
_UNKNOWN_SHAPE = (
    "The stories file is in a shape this Beacon does not know: a newer one wrote it. "
    "It is left as it is. Run a Beacon that knows the shape. Do not delete the file "
    "or move it away to begin another: it is the record of which lines were paid "
    "for, and without it the same lines are paid for again."
)


class StoriesError(Exception):
    """The stories file cannot be used as asked. The text is fixed."""


@dataclass(frozen=True)
class Story:
    """The story of an incident, as it is kept and shown."""

    incident_id: str
    # Names the lines it was written from.
    basis: str
    written_at: datetime
    claims: tuple[Claim, ...]
    # How many of the points Ember gave were left out, and how many of the
    # incident's older lines Ember was not given.
    left_out: int
    lines: int
    lines_left_out: int
    # Which kind of Ember client chose its points: `live`, or `fake` for a stand-in.
    model: str
    # Whether a stand-in had a part in it: a fake Ember chose its points, or a line
    # it was made from is a stand-in's.
    stand_in: bool
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int


class Stories:
    """One file of stories. It is safe to share between threads."""

    def __init__(self, path: Path, mode: Mode) -> None:
        """Raises StoriesError when the file was begun as the other mode's, and
        when it is in a shape this Beacon does not know. A file that is refused is
        left as it was."""
        self._path = path
        self._mode = mode
        self._closing = threading.Lock()
        self._closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        _begin(path, mode)

    @property
    def mode(self) -> Mode:
        return self._mode

    def close(self) -> None:
        """Write nothing more. A write that is under way is finished first. Reading
        goes on working."""
        with self._closing:
            self._closed = True

    def claim(self, incident_id: str, basis: str, now: datetime, interval: timedelta) -> bool:
        """Claim the story of an incident's basis, and say whether the claim is
        this caller's. It is False when the basis was claimed before, by anyone,
        and when the incident's newest claim is younger than `interval`.

        Raises StoriesError when the file is closed."""
        moment = _stamp(now)
        with self._writing() as db:
            newest = db.execute(
                "SELECT MAX(claimed_at) FROM story WHERE incident_id = ?", (incident_id,)
            ).fetchone()[0]
            if newest is not None and newest > _stamp(now - interval):
                return False
            taken = db.execute(
                "SELECT 1 FROM story WHERE incident_id = ? AND basis = ?", (incident_id, basis)
            ).fetchone()
            if taken is not None:
                return False
            db.execute("DELETE FROM story WHERE claimed_at < ?", (_stamp(now - KEPT),))
            db.execute(
                "INSERT INTO story (incident_id, basis, claimed_at) VALUES (?, ?, ?)",
                (incident_id, basis, moment),
            )
        return True

    def release(self, incident_id: str, basis: str) -> None:
        """Give a claim up, so the basis can be claimed again. Only the caller whose
        claim it is does this, and only when nothing was paid under it.

        Raises StoriesError when the file is closed. The claim then stays."""
        with self._writing() as db:
            db.execute(
                "DELETE FROM story WHERE incident_id = ? AND basis = ? AND story IS NULL",
                (incident_id, basis),
            )

    def write(
        self,
        incident_id: str,
        basis: str,
        draft: Draft,
        *,
        stand_in: bool,
        now: datetime,
        state: Mapping[str, Any] | None = None,
    ) -> Story:
        """Keep the story written under a claim, and return it. `state` is what the
        caller must remember between merges, saved in the same transaction.

        Raises StoriesError when the live file is handed a stand-in's story, when
        the basis is not claimed, and when the file is closed."""
        if self._mode == "live" and (draft.model != "live" or stand_in):
            raise_bare(StoriesError("The live stories file holds nothing a stand-in wrote."))
        story = Story(
            incident_id=incident_id,
            basis=basis,
            written_at=now.astimezone(UTC),
            claims=draft.claims,
            left_out=draft.left_out,
            lines=draft.lines,
            lines_left_out=draft.lines_left_out,
            model=draft.model,
            stand_in=stand_in or draft.model != "live",
            input_tokens=draft.input_tokens,
            cached_tokens=draft.cached_tokens,
            output_tokens=draft.output_tokens,
            reasoning_tokens=draft.reasoning_tokens,
            cost_usd=draft.cost_usd,
            latency_ms=draft.latency_ms,
        )
        with self._writing() as db:
            changed = db.execute(
                "UPDATE story SET written_at = ?, story = ? "
                "WHERE incident_id = ? AND basis = ? AND story IS NULL",
                (_stamp(now), json.dumps(_as_json(story)), incident_id, basis),
            ).rowcount
            if changed != 1:
                raise_bare(StoriesError("A story is written only under a claim, once."))
            if state is not None:
                _save_state(db, state)
        return story

    def latest(self, incident_id: str) -> Story | None:
        """The story of an incident that was written last, or None."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT basis, story FROM story WHERE incident_id = ? AND story IS NOT NULL "
                "ORDER BY written_at DESC, rowid DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
        return None if row is None else _from_json(incident_id, row[0], json.loads(row[1]))

    def state(self) -> dict[str, Any]:
        """What `save_state` or `write` saved last, or an empty mapping."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'state'").fetchone()
        return {} if row is None else json.loads(row[0])

    def save_state(self, state: Mapping[str, Any]) -> None:
        """Raises StoriesError when the file is closed."""
        with self._writing() as db:
            _save_state(db, state)

    def _connect(self) -> sqlite3.Connection:
        # A connection for each operation, as the evidence store does.
        db = sqlite3.connect(self._path, timeout=_WAIT_SECONDS, isolation_level=None)
        db.execute("PRAGMA journal_mode = WAL")
        return db

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """One write, begun only while the file is open and committed before
        `close` returns."""
        with self._closing:
            if self._closed:
                raise_bare(StoriesError("The stories file is closed, and takes no more."))
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")


def _begin(path: Path, mode: Mode) -> None:
    """Make the file at `path` this mode's stories file in this shape, or refuse it.

    Everything is decided and done in one transaction that holds the file's write
    lock from before the file is first read, so no other process can change the
    file between the look and the act:

    - A file in this shape and of this mode is taken as it is.
    - A new file, and a file of an earlier shape, are made this shape. The stories
      of an earlier shape are emptied and never read: they cannot be read as this
      one, the first held sentences a model wrote, and the next held words that
      have since changed. Each row stays, as a claim: its lines were paid for once,
      and are not paid for again.
    - A file in any other shape, and a file of the other mode, are refused, and
      **nothing of a refused file is written**: no table, no row, no journal mode.

    Raises StoriesError for a file that is refused."""
    refusal: str | None
    # Not `_connect`: that sets the journal mode, which is a write.
    with closing(sqlite3.connect(path, timeout=_WAIT_SECONDS, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            shape = db.execute("PRAGMA user_version").fetchone()[0]
            refusal = _refusal(db, shape, mode)
            if refusal is None:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('mode', ?)", (mode,))
                if shape != _FORMAT:
                    db.execute(
                        "UPDATE story SET story = NULL, written_at = NULL WHERE story IS NOT NULL"
                    )
                    db.execute(f"PRAGMA user_version = {_FORMAT}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("ROLLBACK" if refusal is not None else "COMMIT")
        if refusal is None:
            db.execute("PRAGMA journal_mode = WAL")
    if refusal is not None:
        raise StoriesError(refusal)


def _refusal(db: sqlite3.Connection, shape: int, mode: Mode) -> str | None:
    """Why a file is not this mode's stories file, or None when it is or can be
    made it. It reads and writes nothing."""
    if shape != _FORMAT and shape not in _EARLIER:
        return _UNKNOWN_SHAPE
    has_meta = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    begun = db.execute("SELECT value FROM meta WHERE key = 'mode'").fetchone() if has_meta else None
    if begun is not None and begun[0] != mode:
        return f"The stories file was begun as the {begun[0]} one, and cannot be the {mode} one."
    return None


def _save_state(db: sqlite3.Connection, state: Mapping[str, Any]) -> None:
    db.execute(
        "INSERT INTO meta (key, value) VALUES ('state', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (json.dumps(dict(state)),),
    )


def _stamp(moment: datetime) -> str:
    """A moment as text that sorts as time does: UTC, always with microseconds."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _as_json(story: Story) -> dict[str, Any]:
    return {
        "written_at": story.written_at.isoformat(),
        "claims": [
            {
                "kind": claim.kind,
                "text": claim.text,
                "at": claim.at.isoformat(),
                "confidence": claim.confidence,
                "sources": [_source_json(source) for source in claim.sources],
            }
            for claim in story.claims
        ],
        "left_out": story.left_out,
        "lines": story.lines,
        "lines_left_out": story.lines_left_out,
        "model": story.model,
        "stand_in": story.stand_in,
        "input_tokens": story.input_tokens,
        "cached_tokens": story.cached_tokens,
        "output_tokens": story.output_tokens,
        "reasoning_tokens": story.reasoning_tokens,
        "cost_usd": story.cost_usd,
        "latency_ms": story.latency_ms,
    }


def _from_json(incident_id: str, basis: str, kept: dict[str, Any]) -> Story:
    return Story(
        incident_id=incident_id,
        basis=basis,
        written_at=datetime.fromisoformat(kept["written_at"]),
        claims=tuple(
            Claim(
                kind=claim["kind"],
                text=claim["text"],
                at=datetime.fromisoformat(claim["at"]),
                confidence=claim["confidence"],
                sources=tuple(_source_from(source) for source in claim["sources"]),
            )
            for claim in kept["claims"]
        ),
        left_out=kept["left_out"],
        lines=kept["lines"],
        lines_left_out=kept["lines_left_out"],
        model=kept["model"],
        stand_in=kept["stand_in"],
        input_tokens=kept["input_tokens"],
        cached_tokens=kept["cached_tokens"],
        output_tokens=kept["output_tokens"],
        reasoning_tokens=kept["reasoning_tokens"],
        cost_usd=kept["cost_usd"],
        latency_ms=kept["latency_ms"],
    )


def _source_json(source: Source) -> dict[str, Any]:
    """A source line as it is kept: which row, which feed, and how far it is
    trusted. It holds nothing the row said."""
    return {
        "kind": source.kind,
        "source": source.source,
        "label": source.label,
        "confidence": source.confidence,
        "at": source.at.isoformat(),
        "evidence_id": source.evidence_id,
        "url": source.url,
        "stand_in": source.stand_in,
        "model": source.model,
        "credit": None
        if source.credit is None
        else {"text": source.credit.text, "url": source.credit.url},
    }


def _source_from(kept: dict[str, Any]) -> Source:
    credit = kept["credit"]
    return Source(
        kind=kept["kind"],
        source=kept["source"],
        label=kept["label"],
        confidence=kept["confidence"],
        at=datetime.fromisoformat(kept["at"]),
        evidence_id=kept["evidence_id"],
        url=kept["url"],
        stand_in=kept["stand_in"],
        model=kept["model"],
        credit=None if credit is None else Credit(credit["text"], credit["url"]),
    )
