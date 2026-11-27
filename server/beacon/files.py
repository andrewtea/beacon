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

`change_as_it_is` is the other way into such a file, for upkeep of one that nothing
has open for use (a store that was set aside, another store's file a command only
deletes rows from): it runs given statements on a file that is seen, in the same
write transaction, to be the kind, the mode, and a shape they were written for,
and makes and migrates nothing.
"""

import sqlite3
from collections.abc import Collection, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

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


@dataclass(frozen=True)
class Look:
    """What a SQLite file says of itself, read without writing to it."""

    # The mode written in its table of that name, or None when it says none.
    mode: str | None
    # Its `user_version`: 0 for a file that says no shape.
    shape: int
    # The names of its tables.
    tables: frozenset[str]
    # How many things its schema holds in all: tables, indexes, views, triggers.
    # None at all is a file that was begun and never written to.
    objects: int


def look(path: Path, *, meta: str = "meta") -> Look:
    """Read what the file at `path` says of itself, through a connection that
    cannot write: its mode, from the table `meta` (key, value), and its shape.
    `begin_file` is how a file is opened for use; this is for deciding about a
    file without changing it, as before a deletion. Nothing is written to the
    file. To read one that is in write-ahead mode, SQLite can make the `-shm` file
    and an empty `-wal` beside it, as any reader of it does.

    Raises sqlite3.Error for a file that is no SQLite database or cannot be read."""
    if not meta.isidentifier():
        raise ValueError("meta names a table")
    address = f"file:{quote(str(path))}?mode=ro"
    with closing(sqlite3.connect(address, uri=True, timeout=_WAIT_SECONDS)) as db:
        shape = db.execute("PRAGMA user_version").fetchone()[0]
        schema = db.execute("SELECT type, name FROM sqlite_master").fetchall()
        tables = frozenset(name for kind, name in schema if kind == "table")
        said = None
        if meta in tables:
            said = db.execute(f"SELECT value FROM {meta} WHERE key = 'mode'").fetchone()
    return Look(
        mode=None if said is None else said[0], shape=shape, tables=tables, objects=len(schema)
    )


# The tables SQLite makes by itself in a file, which are no kind's of Beacon's.
SQLITES_OWN = frozenset(
    {"sqlite_sequence", "sqlite_stat1", "sqlite_stat2", "sqlite_stat3", "sqlite_stat4"}
)


def change_as_it_is(
    path: Path,
    mode: Mode,
    *,
    shapes: Collection[int],
    tables: Collection[str],
    own: str,
    statements: Sequence[tuple[str, Sequence[str]]],
    meta: str = "meta",
) -> int | None:
    """Run `statements` on the file at `path` as the file is, and say how many
    rows they changed, or None, with nothing written, when the file is not what
    was asked for.

    For upkeep of a file that nothing has open for use: one that was set aside, or
    another store's that a command only deletes rows from. `begin_file` is how a
    file is opened for use, and makes a file this Beacon's shape; this makes no
    table, drops nothing of an earlier shape, and leaves the file's shape number
    and journal mode as they are. The file has to be there (none is made), say it
    is `mode`'s in the table `meta`, be in one of `shapes`, hold the table `own`,
    and hold no table that is not in `tables` or SQLite's own: a kind is known by
    its tables, since several of Beacon's files say a mode in the same table with
    the same shape number. **The look and the change are one write transaction**,
    as `begin_file`'s are.

    Raises sqlite3.Error for a file that is no SQLite database, is not there, or
    cannot be written to."""
    if not meta.isidentifier():
        raise ValueError("meta names a table")
    # `mode=rw`: a file that is not there is not made.
    address = f"file:{quote(str(path))}?mode=rw"
    with closing(
        sqlite3.connect(address, uri=True, timeout=_WAIT_SECONDS, isolation_level=None)
    ) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            shape = db.execute("PRAGMA user_version").fetchone()[0]
            schema = db.execute("SELECT type, name FROM sqlite_master").fetchall()
            found = frozenset(name for kind, name in schema if kind == "table")
            said = None
            if meta in found:
                said = db.execute(f"SELECT value FROM {meta} WHERE key = 'mode'").fetchone()
            asked_for = (
                said is not None
                and said[0] == mode
                and shape in shapes
                and own in found
                and found - SQLITES_OWN <= frozenset(tables)
            )
            changed = 0
            if asked_for:
                for statement, values in statements:
                    changed += db.execute(statement, tuple(values)).rowcount
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT" if asked_for else "ROLLBACK")
    return changed if asked_for else None
