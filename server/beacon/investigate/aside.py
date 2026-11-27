"""The investigations of mock stores that were set aside, deleted when their day
is over.

Setting a mock store aside (`beacon.serve.set_aside`) moves its investigations
file under `told/` in the data directory with the rest, and deletes nothing. A run
and a decision on crisis mode are kept for a day and no longer (`store.KEPT`), and
a tap's id, which is what its sender reads its answer with, is in each run. So
whichever server runs on the data directory deletes from those files what is older
than that, on the same pass that sweeps its own (`main._sweep_runs`), in whichever
mode it runs: what a file holds does not outlast its day because the file was
moved.

**A file is swept as it is** (`beacon.files.change_as_it_is`): only when it says
it is the mock store's investigations, by its tables, its mode, and a shape whose
rows the sweep's statements delete from (`store.SWEPT_SHAPES`), and with no table
made in it and nothing of an earlier shape dropped. Any other is left and counted.

It is the investigations only. The claims in the other files that were set aside
name no tap.
"""

import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

from beacon.datadir import ASIDE, ASIDE_NAME
from beacon.files import change_as_it_is
from beacon.investigate import store

# What SQLite keeps beside a database, which it opens with it.
_BESIDE = ("-wal", "-shm", "-journal")


def sweep_aside(data: Path, now: datetime) -> tuple[int, int]:
    """Delete, from the investigations file of every mock store that was set
    aside under `data`, the runs and decisions older than they are kept. Answers
    how many rows went, and how many files could not be swept.

    A file is opened only when it and what SQLite keeps beside it are files of
    their own, in directories of their own under `told/`, of the names the
    command that sets a store aside gives: a link there could be to anything.
    A file that could not be looked at is not taken to be absent: it is counted
    among those that could not be swept. Whoever calls holds the data
    directory's lock, which the command that moves files needs alone."""
    root = data / ASIDE
    try:
        if not _is_directory(root):
            return 0, 0
        names = sorted(name for name in os.listdir(root) if ASIDE_NAME.fullmatch(name))
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return 0, 1
    deleted = unswept = 0
    for name in names:
        file = root / name / "investigate" / "mock.sqlite3"
        try:
            if not (_is_directory(root / name) and _is_directory(file.parent) and _own(file)):
                unswept += 1
                continue
            went = change_as_it_is(
                file,
                "mock",
                shapes=store.SWEPT_SHAPES,
                tables=store.TABLES,
                own=store.OWN_TABLE,
                statements=store.swept(now),
            )
        except FileNotFoundError:
            # A store with no investigations, or a directory that has gone.
            continue
        except (sqlite3.Error, OSError):
            unswept += 1
            continue
        if went is None:
            unswept += 1
        else:
            deleted += went
    return deleted, unswept


def _is_directory(path: Path) -> bool:
    """Whether a directory of its own is at the path.

    Raises FileNotFoundError when nothing is, and OSError when it could not be
    looked at."""
    return stat.S_ISDIR(os.lstat(path).st_mode)


def _own(file: Path) -> bool:
    """Whether a database and each thing SQLite keeps beside it is a file of its
    own: no link of either kind.

    Raises FileNotFoundError when there is no database, and OSError when one of
    them could not be looked at."""
    for each in (file, *(file.with_name(file.name + suffix) for suffix in _BESIDE)):
        try:
            found = os.lstat(each)
        except FileNotFoundError:
            if each == file:
                raise
            continue
        if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
            return False
    return True
