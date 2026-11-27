"""Beginning a SQLite file that is one mode's, in one shape.

Beacon keeps what it paid Ember for in files of its own beside the evidence store:
the stories of incidents (`beacon.story.store`) and the investigations of taps
(`beacon.investigate.store`). Each is the mock store's or the live store's for life,
and each says in its `user_version` which shape its rows have. `begin_file` opens
such a file for the first time in a process, and is where the rules about that are
written once:

- **The look and the change are one transaction**, which holds the file's write
  lock from before the file is first read. No other process can change the file
  between the two.
- **A file in this shape and of this mode is taken as it is.**
- **A new file, and a file of an earlier shape, are made this shape.** What a file
  of an earlier shape held that this one cannot read is dropped by the statements
  the caller gives, and the rest, which is what was claimed and paid for, stays.
  An earlier shape whose rows this one reads as they are (`readable`) is given this
  shape's number and loses nothing. It gets the number all the same: an older
  Beacon, which would read this one's rows without what they have gained, then
  refuses the file instead.
- **A file in any other shape, and a file of the other mode, are refused, and
  nothing of a refused file is written**: no table, no row, no journal mode. A file
  a newer Beacon wrote is the record of what it paid for, and is not this one's to
  empty.
"""

import sqlite3
from collections.abc import Collection, Sequence
from contextlib import closing
from pathlib import Path
from typing import Literal

Mode = Literal["live", "mock"]

_WAIT_SECONDS = 5.0


def begin_file(
    path: Path,
    mode: Mode,
    *,
    what: str,
    schema: Sequence[str],
    shape: int,
    earlier: Collection[int],
    drop: Sequence[str],
    unknown: str,
    readable: Collection[int] = (),
) -> str | None:
    """Make the file at `path` the `mode` one in `shape`, or say why it is refused.

    `schema` is the file's tables, one statement each, each of which changes nothing
    when its table is there. It has a table `meta (key, value)`, in which the file's
    mode is kept. `earlier` are the shapes this one can be made from, with 0 for a
    file that says none, which a new file is too; `drop` is run on a file of one of
    them. `readable` are the earlier shapes whose rows this one reads as they are:
    a file of one of them gets this shape's number, and `drop` is not run on it.
    `what` names the file in a refusal (`stories`), and `unknown` is the
    refusal of a file in a shape that is neither.

    Returns None for a file that is now this mode's and this shape's, and else the
    sentence to refuse it with. A refused file is left as it was, to the byte."""
    refusal: str | None
    # A connection that sets no journal mode: setting one is a write.
    with closing(sqlite3.connect(path, timeout=_WAIT_SECONDS, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            found = db.execute("PRAGMA user_version").fetchone()[0]
            known = {*earlier, *readable}
            refusal = _refusal(db, found, mode, what, shape, known, unknown)
            if refusal is None:
                for statement in schema:
                    db.execute(statement)
                db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('mode', ?)", (mode,))
                if found != shape:
                    if found not in readable:
                        for statement in drop:
                            db.execute(statement)
                    db.execute(f"PRAGMA user_version = {int(shape)}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("ROLLBACK" if refusal is not None else "COMMIT")
        if refusal is None:
            db.execute("PRAGMA journal_mode = WAL")
    return refusal


def _refusal(
    db: sqlite3.Connection,
    found: int,
    mode: Mode,
    what: str,
    shape: int,
    earlier: Collection[int],
    unknown: str,
) -> str | None:
    """Why a file is not this mode's file in this shape, or None when it is or can
    be made it. It writes nothing."""
    if found != shape and found not in earlier:
        return unknown
    has_meta = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    begun = db.execute("SELECT value FROM meta WHERE key = 'mode'").fetchone() if has_meta else None
    if begun is not None and begun[0] != mode:
        return f"The {what} file was begun as the {begun[0]} one, and cannot be the {mode} one."
    return None
