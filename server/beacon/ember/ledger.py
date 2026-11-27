"""The Ember call ledger: one SQLite row per call, and the JSONL trace exported from it.

The `ember_calls` table is the only record of what was called and spent through this
client. The spending stop reads it, and `export_trace` writes it out as the prompt and
token trace for the hackathon submission. Each row holds its request and response with
media redacted, so the trace can always be rebuilt from the table.

A live call is written twice. Before the request is sent, `reserve` inserts a `pending`
row that holds the request and the most the call can cost, and refuses the call if that
could take live spend past the budget. Afterwards, `settle` replaces the reservation
with the real cost. Because the check and the insert share one transaction, parallel
calls cannot overshoot the budget together, and a call that crashes mid-flight keeps its
reservation: its real cost is unknown, so it is counted at the maximum.

A call can carry a *scope*: the name of a piece of work with a budget of its own, such
as one tap's investigation or the scale run. `reserve` then also refuses a call that
could take the scope's live spend past the scope's budget, in the same transaction. A
scope's spend is the sum of its own rows, so no other client's calls move it, and it
holds over every process that uses the scope for as long as the ledger file lasts.

A ledger knows the file it was opened on by two things: the generation written in the
file when it was begun, and which file is at the path (its device and inode). A file
that is deleted and begun again has a new generation. A file that is put back from a
copy by renaming keeps its generation and is a different file. Either stops paid calls
(`reserve`) and leaves a reservation made before it unsettled (`settle`). What neither
shows is a file rolled back in place, as a filesystem snapshot or a copy written over
the same file does: the ledger then holds less than was spent and nothing here can
tell. Whoever restores a ledger has to stop every process that spends first, and
compare the ledger with Meta's Usage page before starting them again.

A ledger begins a file where it finds none, which is right for a first call and
wrong for whoever means to go on paying from the ledger that was paid from before.
**A ledger opened on a file that was named to it (`only`) never begins one, and
never reads or writes another**: every connection it makes, when it is opened and
for as long as it is used, is to that file, seen to be it by device, inode, and
generation, and makes no file and no table. A file that is not the named one
refuses it (`OtherLedgerError`), and when the file goes from under it afterwards,
or another ledger is put at the path, it is a replaced ledger to every reader and
writer, with nothing put in its place and nothing read from or logged in the other.
"""

import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import quote

from beacon.ember.errors import BudgetExceededError, LedgerReplacedError, OtherLedgerError
from beacon.ember.pricing import MODEL, Usage, to_usd
from beacon.errors import raise_bare

Source = Literal["live", "replay", "fake"]
# ok: answered, cost is what the tokens cost. error: the call failed and its cost is
# known, which is zero when the request never left this machine. unknown: no usable
# answer and the call may have been billed, so the row keeps its reservation.
# pending: in flight, or the process died mid-call.
Status = Literal["pending", "ok", "error", "unknown"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ember_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('live', 'replay', 'fake')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'error', 'unknown')),
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    input_tokens INTEGER,
    cached_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost_nanousd INTEGER NOT NULL,
    latency_ms INTEGER,
    response_id TEXT,
    error TEXT,
    request TEXT NOT NULL,
    response TEXT,
    scope TEXT,
    token TEXT NOT NULL
)
"""
# A ledger's row ids start again when its file is replaced. The generation is drawn
# once in a ledger file's life, so a row id kept elsewhere can be told from the same
# number in a later file.
_META = "CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_GENERATION = "SELECT value FROM ledger_meta WHERE key = 'generation'"


class RowRef(NamedTuple):
    """One call's row. A row id alone does not name a call, because a ledger file that
    is deleted and begun again counts its rows from one again, and neither does the
    id beside the file's generation, because an older copy of a file that is put back
    has the same generation and gives the same ids to different calls. The token is
    drawn for each row when it is written, so two rows are never told apart by less."""

    generation: str
    id: int
    token: str


_INSERT = """
INSERT INTO ember_calls (
    started_at, source, status, task, model, effort, max_output_tokens, request_hash,
    input_tokens, cached_tokens, output_tokens, reasoning_tokens,
    cost_nanousd, latency_ms, response_id, request, response, scope, token
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LIVE_SPEND = "SELECT COALESCE(SUM(cost_nanousd), 0) FROM ember_calls WHERE source = 'live'"
_SCOPE_SPEND = _LIVE_SPEND + " AND scope = ?"


@dataclass(frozen=True)
class Call:
    """What is known about a call before it is answered."""

    request_hash: str
    task: str
    effort: str
    max_output_tokens: int
    # The request body with media redacted.
    request: Mapping[str, Any]
    # The piece of work the call belongs to, when it has a budget of its own.
    scope: str | None = None


@dataclass(frozen=True)
class ScopeSpend:
    """What a scope's live calls came to, in one ledger file."""

    # The generation of the ledger file these were read from.
    generation: str
    # Live calls, each attempt counted, and how many of them were not answered: in
    # flight, failed, or of unknown outcome.
    calls: int
    unanswered: int
    # Their cost, counting an unanswered call that may have been billed at its maximum.
    spent_nanousd: int


@dataclass(frozen=True)
class LedgerCall:
    """One row as a person reads it: what was called, how long it took, and what it
    cost. The request and the response stay in the ledger."""

    id: int
    started_at: str
    source: Source
    status: Status
    task: str
    model: str
    effort: str
    request_hash: str
    # None for a call that was not answered.
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    # What the tokens cost at Ember's rates. Only a `live` row spent it.
    cost_usd: float
    latency_ms: int | None


_SHOWN = (
    "id, started_at, source, status, task, model, effort, request_hash, input_tokens, "
    "cached_tokens, output_tokens, reasoning_tokens, cost_nanousd, latency_ms"
)


_REPLACED = (
    "Refused: the Ember ledger file was deleted or replaced while this client "
    "was running, so what has been spent is not known from it. No paid call is "
    "made until the spend is accounted for and the server is started again."
)
_OTHER_LEDGER = (
    "The ledger file this client was to go on with is not what is at the ledger's "
    "path: another ledger is there, a copy, something that is no ledger, or nothing. "
    "No ledger was begun and nothing was written."
)


class Ledger:
    def __init__(self, db_path: Path, *, only: str | None = None) -> None:
        """Open the ledger at `db_path`, beginning one when none is there.

        `only` is for whoever goes on with a ledger that was paid from before: the
        name of its file, as `opened_file` and `kept_file` give it. The ledger is
        then opened only when the file at the path is that one, and **neither now
        nor later does it begin a file or make a table, or read or write another
        file**: every connection is to that file (`mode=rw`, its device, inode, and
        generation seen each time) or is not made. When the file goes from under
        it, or another is put in its place, a ledger or a copy of this one, what
        reads it or writes to it raises LedgerReplacedError (`spent_nanousd`,
        `generation`, `scope_spend`, `reserve`, `record`, `export_trace`), `settle`
        and `is_opened_file` answer False, and `calls` answers none.

        Raises OtherLedgerError, having made nothing, when `only` is given and what
        is at the path is not that file."""
        self._db_path = db_path
        self._only = only is not None
        if only is None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # The file this ledger was opened on. Live spend is counted in it and in
            # no other: see `reserve`.
            with closing(self._connect()) as db:
                self._opened = _generation(db)
                self._identity = _identity(db_path)
            return
        # Which file it is, from its name, before anything is opened: every
        # connection is then to that file or to none (`_connect_existing`).
        named = FILE_NAME.fullmatch(only) is not None
        generation, _, where = only.partition(":")
        device, _, inode = where.partition(":")
        self._opened = generation
        self._identity = (int(device), int(inode)) if named else None
        there = False
        if named:
            try:
                self._connect().close()
                there = True
            except (sqlite3.Error, LedgerReplacedError):
                # Not under the error raised below.
                pass
        if not there:
            raise_bare(OtherLedgerError(_OTHER_LEDGER))

    def spent_nanousd(self) -> int:
        """Live spend so far, counting calls with an unknown outcome at their maximum."""
        with closing(self._connect()) as db:
            return db.execute(_LIVE_SPEND).fetchone()[0]

    def generation(self) -> str:
        """The generation of the ledger file at this path now."""
        with closing(self._connect()) as db:
            return _generation(db)

    def scope_spend(self, scope: str) -> ScopeSpend:
        """What the scope's live calls came to in the file at this path now."""
        with closing(self._connect()) as db:
            calls, unanswered, spent = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(status != 'ok'), 0), "
                "COALESCE(SUM(cost_nanousd), 0) FROM ember_calls "
                "WHERE source = 'live' AND scope = ?",
                (scope,),
            ).fetchone()
            return ScopeSpend(_generation(db), calls, unanswered, spent)

    def reserve(
        self,
        call: Call,
        *,
        reserved_nanousd: int,
        budget_nanousd: int,
        scope_budget_nanousd: int | None = None,
    ) -> RowRef:
        """Admit a live call that can cost at most `reserved_nanousd`, and return its
        row. Raises BudgetExceededError when the call could take live spend past the
        budget, or the live spend of the call's scope past `scope_budget_nanousd`.

        Raises LedgerReplacedError, which is one, when the file is no longer the one
        this ledger was opened on. A deleted ledger begins again empty, and a paid
        call admitted against it would be measured from zero with the earlier spend
        forgotten. Calls that spend nothing are still logged in the new file. Paid
        calls stay refused until the process is started again, which is for whoever
        deleted the file to do once they have accounted for what was spent."""
        with self._transaction() as db:
            self._require_opened_file(db)
            spent = db.execute(_LIVE_SPEND).fetchone()[0]
            if spent + reserved_nanousd > budget_nanousd:
                raise BudgetExceededError(
                    f"Refused: the Ember ledger holds ${to_usd(spent):.6f} of live spend, and "
                    f"this call could add ${to_usd(reserved_nanousd):.6f}, which would pass "
                    f"the ${to_usd(budget_nanousd):.2f} stop."
                )
            if scope_budget_nanousd is not None:
                if call.scope is None:
                    raise ValueError("A scope's budget needs a call that names its scope.")
                scoped = db.execute(_SCOPE_SPEND, (call.scope,)).fetchone()[0]
                if scoped + reserved_nanousd > scope_budget_nanousd:
                    raise BudgetExceededError(
                        f"Refused: {call.scope} holds ${to_usd(scoped):.6f} of live spend "
                        f"in the Ember ledger, and this call could add "
                        f"${to_usd(reserved_nanousd):.6f}, which would pass its "
                        f"${to_usd(scope_budget_nanousd):.6f} budget."
                    )
            token = secrets.token_hex(8)
            cursor = db.execute(
                _INSERT,
                _values(call, "live", "pending", None, reserved_nanousd, None, None, token),
            )
            return RowRef(_generation(db), cursor.lastrowid, token)

    def require_opened_file(self) -> None:
        """Raise LedgerReplacedError when the file is not the one this ledger was
        opened on. `reserve` checks again, in the transaction that admits the call."""
        with closing(self._connect()) as db:
            self._require_opened_file(db)

    def is_opened_file(self) -> bool:
        """Whether the file at the path now is the one this ledger was opened on.
        What a read of the path returns (`scope_spend`, `generation`) is of that
        file only when it is."""
        try:
            with closing(self._connect()) as db:
                return self._is_opened_file(db)
        except LedgerReplacedError:
            # The file that was named is not at the path, for a ledger that was named one.
            return False

    def opened_file(self) -> str:
        """A name for the file this ledger was opened on: its generation, its
        device, and its inode. Another file, a copy put back by renaming among
        them, has another name."""
        device, inode = self._identity if self._identity is not None else (0, 0)
        return f"{self._opened}:{device}:{inode}"

    def _is_opened_file(self, db: sqlite3.Connection) -> bool:
        # The generation tells a file that was begun again. A file put back from an
        # older copy by renaming has the same generation and is another file.
        return _generation(db) == self._opened and _identity(self._db_path) == self._identity

    def _require_opened_file(self, db: sqlite3.Connection) -> None:
        if not self._is_opened_file(db):
            raise LedgerReplacedError(_REPLACED)

    def settle(
        self,
        row: RowRef,
        *,
        status: Literal["ok", "error", "unknown"],
        cost_nanousd: int,
        latency_ms: int,
        usage: Usage | None = None,
        response: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Replace a live call's reservation with its outcome, and return whether it
        was written. `response` must already be redacted. A reservation made in a
        ledger file that has since been replaced went with that file: nothing is
        written, and the answer is False. That holds when the file now at the path
        has a row of the same id, as an older copy that was put back can: the row
        is another call's and has another token."""
        try:
            return self._settle(row, status, cost_nanousd, latency_ms, usage, response, error)
        except LedgerReplacedError:
            # The file that was named is not at the path, for a ledger that was named one.
            return False

    def _settle(
        self,
        row: RowRef,
        status: Literal["ok", "error", "unknown"],
        cost_nanousd: int,
        latency_ms: int,
        usage: Usage | None,
        response: Mapping[str, Any] | None,
        error: str | None,
    ) -> bool:
        with self._transaction() as db:
            if not self._is_opened_file(db):
                return False
            cursor = db.execute(
                """
                UPDATE ember_calls SET
                    status = ?, cost_nanousd = ?, latency_ms = ?, input_tokens = ?,
                    cached_tokens = ?, output_tokens = ?, reasoning_tokens = ?,
                    response_id = ?, response = ?, error = ?
                WHERE id = ? AND token = ?
                    AND ? = (SELECT value FROM ledger_meta WHERE key = 'generation')
                """,
                (
                    status,
                    cost_nanousd,
                    latency_ms,
                    *_token_columns(usage),
                    _response_id(response),
                    _json(response),
                    error,
                    row.id,
                    row.token,
                    row.generation,
                ),
            )
            return cursor.rowcount == 1

    def record(
        self,
        call: Call,
        *,
        source: Literal["replay", "fake"],
        usage: Usage,
        latency_ms: int,
        response: Mapping[str, Any],
    ) -> RowRef:
        """Log a call that spent nothing, and return its row. Its cost column is what
        the tokens would cost live, so replayed and scripted runs report realistic
        numbers; only rows with source 'live' count as spend. `response` must already
        be redacted."""
        with self._transaction() as db:
            token = secrets.token_hex(8)
            cursor = db.execute(
                _INSERT,
                _values(call, source, "ok", usage, usage.cost_nanousd, latency_ms, response, token),
            )
            return RowRef(_generation(db), cursor.lastrowid, token)

    def calls(self, refs: Sequence[RowRef]) -> list[LedgerCall]:
        """The rows these name, in the order the calls started. One with no row is
        left out, and so is one from another ledger file than the one at this path
        now: the file's generation is read on the connection its rows are read on, so
        a row is never taken from a file other than the one it was checked against.
        A row that has the id and not the token is another call's, written at that id
        in an older copy of the file that was put back, and is left out too."""
        if not refs:
            return []
        try:
            db = self._connect()
        except LedgerReplacedError:
            # The file that was named is not at the path, for a ledger that was named one.
            return []
        with closing(db):
            generation = _generation(db)
            wanted = {(ref.id, ref.token) for ref in refs if ref.generation == generation}
            if not wanted:
                return []
            ids = sorted({row_id for row_id, _token in wanted})
            marks = ", ".join("?" * len(ids))
            rows = db.execute(
                f"SELECT {_SHOWN}, token FROM ember_calls WHERE id IN ({marks}) ORDER BY id",
                tuple(ids),
            ).fetchall()
        calls = []
        for row in rows:
            shown = dict(row)
            if (shown["id"], shown.pop("token")) in wanted:
                calls.append(LedgerCall(cost_usd=to_usd(shown.pop("cost_nanousd")), **shown))
        return calls

    def export_trace(self, path: Path) -> int:
        """Rewrite `path` as JSONL, one line per call in the order the calls started,
        and return the number of calls. Calls that are in flight, or whose process died,
        are included as `pending`. Running it again gives the same file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a reader never sees a partial file.
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            count = 0
            with os.fdopen(handle, "w", encoding="utf-8") as file, closing(self._connect()) as db:
                for row in db.execute("SELECT * FROM ember_calls ORDER BY id"):
                    line = dict(row)
                    line["cost_usd"] = to_usd(line.pop("cost_nanousd"))
                    line["request"] = json.loads(line["request"])
                    if line["response"] is not None:
                        line["response"] = json.loads(line["response"])
                    file.write(json.dumps(line, ensure_ascii=False) + "\n")
                    count += 1
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return count

    def _connect(self) -> sqlite3.Connection:
        # A connection per operation keeps the ledger safe to share between threads and
        # processes. isolation_level=None leaves transactions to _transaction.
        if self._only:
            return self._connect_existing()
        db = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            # The file can be deleted while a server runs, and SQLite then begins an
            # empty one here. Every connection finds its tables and a generation.
            try:
                begun = db.execute(_GENERATION).fetchone() is not None
            except sqlite3.OperationalError:
                begun = False
            if not begun:
                db.execute(_SCHEMA)
                db.execute(_META)
                # Of two connections that begin a file at once, one generation is kept.
                db.execute(
                    "INSERT OR IGNORE INTO ledger_meta (key, value) VALUES ('generation', ?)",
                    (secrets.token_hex(16),),
                )
        except BaseException:
            db.close()
            raise
        return db

    def _connect_existing(self) -> sqlite3.Connection:
        """A connection to the ledger file that was named, for a ledger that begins
        none (`only`): `mode=rw` opens a file and makes none, and no table is made
        in a file that has none. **It is a connection to the named file or there is
        none**: the file at the path is seen to be that one, by its device and
        inode before the connection is made and again after its generation is read
        on it, and by that generation, so that no reader and no writer is handed
        another ledger that was put at the path, a copy with the generation
        included.

        Raises LedgerReplacedError when the named file is not what is at the path:
        nothing is there, or another file is, a ledger or not."""
        address = f"file:{quote(str(self._db_path))}?mode=rw"
        db = None
        same = False
        if self._named_file_is_there():
            # Nothing that can be opened is there when this fails.
            with suppress(sqlite3.OperationalError):
                db = sqlite3.connect(address, uri=True, timeout=30, isolation_level=None)
        if db is not None:
            db.row_factory = sqlite3.Row
            try:
                found = db.execute(_GENERATION).fetchone()
                same = (
                    found is not None and found[0] == self._opened and self._named_file_is_there()
                )
            except sqlite3.OperationalError as error:
                # A file that was emptied has no such table. Anything else is the
                # file's own trouble, and is raised as it is.
                if "no such table" not in str(error):
                    db.close()
                    raise
            except BaseException:
                db.close()
                raise
        if not same:
            if db is not None:
                db.close()
            raise_bare(LedgerReplacedError(_REPLACED))
        assert db is not None
        return db

    def _named_file_is_there(self) -> bool:
        """Whether what is at the path is the file this ledger was named (`only`):
        a file of its own, no link, with that device and inode."""
        try:
            found = os.lstat(self._db_path)
        except OSError:
            return False
        return stat.S_ISREG(found.st_mode) and (found.st_dev, found.st_ino) == self._identity

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            # IMMEDIATE takes the write lock up front, so a budget check and the insert
            # that follows it see the same rows.
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")


def _generation(db: sqlite3.Connection) -> str:
    """The generation of the file this connection is open on."""
    return db.execute(_GENERATION).fetchone()[0]


# The columns a ledger's accounting is read from: a file without them is no ledger
# this Beacon counts spend in.
_ACCOUNTING = frozenset({"id", "source", "status", "cost_nanousd", "scope", "token"})


# A generation as `_connect` draws one. Nothing else is taken for one, whatever a
# file holds where a generation would be.
_A_GENERATION = re.compile(r"[0-9a-f]{32}")
# A ledger file's name as `Ledger.opened_file` gives it.
FILE_NAME = re.compile(r"[0-9a-f]{32}:[0-9]{1,20}:[0-9]{1,20}")


def kept_file(path: Path) -> str | None:
    """The name of the ledger file at `path` as `Ledger.opened_file` would give it
    (its generation, its device, and its inode), read through a connection that
    cannot write, or None when no ledger that was begun is there: nothing at the
    path, a link, an empty file, a file that is no SQLite database, or one without
    the accounting table or a generation of the shape one is drawn in.

    `Ledger` begins a file when it finds none, or an empty one, which is right for a
    first call and wrong for whoever means to go on paying from the ledger that was
    paid from before: a ledger begun anew counts nothing of what was spent. This is
    how that is asked before anything is made. The name tells a file from a copy of
    it put in its place, which has the generation and can lack rows. **What it
    cannot tell** is a ledger rolled back in place, as a copy written over the same
    file is, or that the ledger is the one its operator means.

    What it answers matches `FILE_NAME`, whatever the file holds: it is put on a
    command line by whoever asks (`scripts/film`)."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            return None
        address = f"file:{quote(str(path))}?mode=ro"
        with closing(sqlite3.connect(address, uri=True, timeout=30)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(ember_calls)")}
            if not columns >= _ACCOUNTING:
                return None
            found = db.execute(_GENERATION).fetchone()
        after = os.lstat(path)
    except (OSError, sqlite3.Error):
        return None
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        # Another file was put there while this one was read.
        return None
    if found is None or not isinstance(found[0], str) or not _A_GENERATION.fullmatch(found[0]):
        return None
    return f"{found[0]}:{after.st_dev}:{after.st_ino}"


def _identity(path: Path) -> tuple[int, int] | None:
    """Which file is at the path now: its device and inode. None when there is none."""
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def _values(
    call: Call,
    source: Source,
    status: Status,
    usage: Usage | None,
    cost_nanousd: int,
    latency_ms: int | None,
    response: Mapping[str, Any] | None,
    token: str,
) -> tuple[Any, ...]:
    return (
        datetime.now(UTC).isoformat(timespec="milliseconds"),
        source,
        status,
        call.task,
        MODEL,
        call.effort,
        call.max_output_tokens,
        call.request_hash,
        *_token_columns(usage),
        cost_nanousd,
        latency_ms,
        _response_id(response),
        _json(call.request),
        _json(response),
        call.scope,
        token,
    )


def _token_columns(usage: Usage | None) -> tuple[int | None, ...]:
    if usage is None:
        return (None, None, None, None)
    return (usage.input_tokens, usage.cached_tokens, usage.output_tokens, usage.reasoning_tokens)


def _response_id(response: Mapping[str, Any] | None) -> str | None:
    identifier = response.get("id") if response else None
    return identifier if isinstance(identifier, str) else None


def _json(value: Mapping[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)
