"""The lock a process holds on a data directory for as long as it has a store open.

The stores under a data directory are files that more than one thing opens: the
pollers write evidence, the server writes taps, and `python -m beacon.serve` deletes
the mock store's files when the invented story is begun again. A file must not be
deleted under a process that has it open, and a process must not open a file, read
what it says, and go on with that after the file was deleted and made again. So a
process takes this lock **before it opens its first store and gives it up after its
last worker has stopped**, and whoever deletes holds it alone.

- **A server that runs the pollers holds it alone** (`shared=False`). No other
  server and no `python -m beacon.ingest` runs on the directory meanwhile: a second
  set of pollers would ask every source twice as often anyway. Only this holder
  may begin the invented story again (`beacon.serve.begin_again`).
- **A server without its pollers (`BEACON_INGEST=0`) and `python -m beacon.ingest`
  hold it together** (`shared=True`), which is how the pollers are run apart from
  the server. Neither deletes a store.

It is a lock on the stores, not on the directory: a scale run, which keeps to files
of its own and the one ledger, takes its own (`beacon.scale.run.alone`), and
`python -m beacon.taps`, which opens no store, takes the taps' (`TapMedia`).
The lock is the operating system's (`flock`), on a file that is never deleted, so
it is gone when its process is, however that ends.

**A mock store that was set aside only in part keeps everything out.** Setting a
store aside (`beacon.serve.set_aside`) moves its files under `told/`, and writes
down where to (`PENDING`) before it moves the first and takes that note away after
the last. While the note is there the lock is refused to everybody but the command
that finishes the move (`HalfAsideError`): a server that started then would begin a
new story beside what is left of the last one's stories and investigations, and a
server that tells the story again would delete those.
"""

import fcntl
import os
import re
import threading
from pathlib import Path

from beacon.errors import raise_bare

LOCK_FILE = ".stores.lock"
# Where a mock store's files are moved to when they are set aside: a directory
# for each time, under this one in the data directory.
ASIDE = "told"
# The note of a move that is under way or was cut short: the name of the directory
# under `ASIDE` it goes into. It is there from before the first file is moved until
# after the last.
PENDING = ".pending"
# What such a directory is named: the moment its store was set aside, and a
# number when two were in one second.
ASIDE_NAME = re.compile(r"\d{8}T\d{6}Z(-\d{1,2})?")


class DataLockedError(Exception):
    """Another process has the data directory's stores, in a way this one cannot
    share. The text is fixed."""


class HalfAsideError(DataLockedError):
    """A mock store under the data directory was set aside only in part: nothing
    opens a store there until the move is finished. The text is fixed."""


HALF_ASIDE = (
    "A mock store under this data directory was being set aside, and the move did "
    f"not finish: some of its files are under {ASIDE}/ and some are where they "
    "were. Nothing that opens a store runs on this data directory until it has. "
    "Give the command again, which moves what is left to the same place: "
    "python -m beacon.serve --set-aside"
)


def aside_pending(data: Path) -> bool:
    """Whether a move of a mock store's files under `ASIDE` was begun and not
    finished. A note that cannot be looked at is taken to be there."""
    try:
        os.lstat(data / ASIDE / PENDING)
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        # `told` is a file: nothing was ever moved into it.
        return False
    except OSError:
        return True
    return True


class DataLock:
    """The lock on one data directory's stores, held until `close`."""

    def __init__(self, data: Path, *, shared: bool, finishing: bool = False) -> None:
        """Take the lock, alone or together with others that share it, without
        waiting. `finishing` is for the command that sets a store aside, which is
        the one thing that runs while a move is unfinished.

        Raises DataLockedError when it is held in a way that excludes this,
        HalfAsideError when a store under the directory was set aside only in
        part, and OSError when the directory or the lock's file cannot be made."""
        self.exclusive = not shared
        self.data = data
        self._closing = threading.Lock()
        data.mkdir(parents=True, exist_ok=True)
        self._file = os.open(data / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        how = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(self._file, how | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._file)
            self._file = -1
            raise_bare(
                DataLockedError(
                    "Another Beacon process has this data directory's stores alone: a "
                    "server that runs the pollers, beside which nothing else that opens a "
                    "store runs. Stop it first. The pollers are run apart from the server "
                    "only beside a server started with BEACON_INGEST=0, both on this one "
                    "data directory."
                    if shared
                    else "Another Beacon process has this data directory's stores open: a "
                    "server, or `python -m beacon.ingest`. A server that runs the pollers "
                    "has them alone. Stop the other first, or start this one without its "
                    "pollers (BEACON_INGEST=0) beside `python -m beacon.ingest`, on this "
                    "one data directory. Do not give either a second data directory: its "
                    "ledger and its claims would be apart from this one's, and the same "
                    "work could be paid for twice."
                )
            )
        except BaseException:
            os.close(self._file)
            self._file = -1
            raise
        # Looked at under the lock: the command that moves holds it alone.
        if not finishing and aside_pending(data):
            os.close(self._file)
            self._file = -1
            raise_bare(HalfAsideError(HALF_ASIDE))

    @property
    def held(self) -> bool:
        return self._file >= 0

    def close(self) -> None:
        """Give the lock up. Closing its file is what releases it."""
        with self._closing:
            if self._file >= 0:
                os.close(self._file)
                self._file = -1
