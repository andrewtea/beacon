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
"""

import fcntl
import os
import threading
from pathlib import Path

from beacon.errors import raise_bare

LOCK_FILE = ".stores.lock"


class DataLockedError(Exception):
    """Another process has the data directory's stores, in a way this one cannot
    share. The text is fixed."""


class DataLock:
    """The lock on one data directory's stores, held until `close`."""

    def __init__(self, data: Path, *, shared: bool) -> None:
        """Take the lock, alone or together with others that share it, without
        waiting.

        Raises DataLockedError when it is held in a way that excludes this, and
        OSError when the directory or the lock's file cannot be made."""
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
                    "Another Beacon process has this data directory's stores: a server "
                    "that runs the pollers has them alone, and nothing else that opens a "
                    "store runs beside it. Stop it first, or give this one a data "
                    "directory of its own (BEACON_DATA_DIR)."
                    if shared
                    else "Another Beacon process has this data directory's stores open: a "
                    "server, or `python -m beacon.ingest`. A server that runs the pollers "
                    "has them alone. Stop the other first, start this one without its "
                    "pollers (BEACON_INGEST=0), or give it a data directory of its own "
                    "(BEACON_DATA_DIR)."
                )
            )
        except BaseException:
            os.close(self._file)
            self._file = -1
            raise

    @property
    def held(self) -> bool:
        return self._file >= 0

    def close(self) -> None:
        """Give the lock up. Closing its file is what releases it."""
        with self._closing:
            if self._file >= 0:
                os.close(self._file)
                self._file = -1
