"""`python -m beacon.taps`: delete the taps' media whose hour is over, once.

    uv run python -m beacon.taps

A running server does this itself, every minute. This is for a data directory whose
server is not running, or cannot be started on the host as it is: the photos and
clips people sent are not left on the disk past their hour for the lack of one. It
takes no tap and reads no file, only how old each is, in both stores' directories.

It ends with 0 when nothing that is over is left, and 1 when the server is running
(which sweeps for itself), a file could not be deleted, or a directory could not
be listed.
"""

import os
from datetime import UTC, datetime

from beacon.ember.client import default_data_dir
from beacon.ember.errors import EmberConfigError
from beacon.taps.media import MediaLockedError, Swept, TapMedia


def sweep_once(now: datetime | None = None) -> Swept:
    """Sweep the media under the data directory the environment names.

    Raises MediaLockedError when a server has the directory, EmberConfigError for
    a relative `BEACON_DATA_DIR`, and OSError when the directory cannot be had."""
    root = default_data_dir(os.environ) / "taps"
    if not root.is_dir():
        # No tap was ever taken here, and a sweep leaves no directory behind.
        return Swept(deleted=0)
    # Either store's name: a sweep covers both directories.
    media = TapMedia(root, "mock")
    try:
        return media.sweep(datetime.now(UTC) if now is None else now)
    finally:
        media.close()


def main() -> None:
    try:
        swept = sweep_once()
    except MediaLockedError:
        raise SystemExit(
            "A Beacon server is running on this data directory, and sweeps it itself."
        ) from None
    except EmberConfigError as error:
        raise SystemExit(str(error)) from error
    except OSError as error:
        # Only the kind of error: its text names a path.
        raise SystemExit(f"The taps' media could not be swept ({type(error).__name__}).") from None
    print(f"Deleted {swept.deleted} files of taps' media whose hour is over.")
    if swept.failed or swept.unlisted:
        raise SystemExit(
            f"{swept.failed} files could not be deleted, and {swept.unlisted} directories "
            "could not be listed. Look at the taps directory."
        )


if __name__ == "__main__":
    main()
