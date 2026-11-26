"""Taking a tap: what a person sends when they press "What's that?".

A tap is a place, the moment it arrived, and optionally a photo or a short clip from
the microphone. It becomes one evidence row of the kind `tap`, which the investigator
starts from and which other taps nearby are counted with. A tap says only that
somebody noticed something, so its row is trusted the least.

A tap is the one thing in the evidence store that is about a person, so little of it
is kept:

- **Where, to about a block.** The position is rounded to three decimal places of a
  degree, which in San Francisco is a box about 110 m by 90 m. The position the
  phone sent is not stored, logged, or repeated in an error.
- **When, by the server's clock.** Nothing the phone says about the time is used.
- **Who: nothing.** No address, no browser, no account, and no name for the phone.
  Two taps cannot be told to be from one person.
- **The photo and the clip**, cleaned of everything but the picture and the sound,
  and for an hour (`beacon.taps.media`). The row says that there was one, how large
  it was, and until when it is kept; it does not hold it.

A tap from outside the area Beacon covers is refused, and nothing of it is kept.
"""

import base64
import binascii
import contextlib
import math
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from beacon.errors import raise_bare
from beacon.evidence import EvidenceStore, EvidenceStoreError, Mode, NewEvidence, Point
from beacon.ingest.area import SAN_FRANCISCO, Bounds
from beacon.taps.media import (
    MAX_CLIP_BYTES,
    MAX_PHOTO_BYTES,
    Clip,
    MediaError,
    MediaFullError,
    MediaKind,
    Photo,
    TapMedia,
    clean_clip,
    clean_photo,
    is_tap_id,
)

# The feed a tap comes from: the web app.
SOURCE = "app"
# The decimal places of a degree a tap's position is kept to.
PLACES = 3
# The most taps taken in any minute, from everybody together. Past it a tap is
# asked to come back: nothing tells one sender from another, so the limit is one
# for all of them.
MAX_TAPS_PER_MINUTE = 120
# The most taps being received at one moment. A tap with a photo and a clip is some
# megabytes in memory while it is read and cleaned, and this bounds how many are.
MAX_TAPS_AT_ONCE = 8
# How far off a phone may say its position is, in metres, at most.
_MAX_ACCURACY_M = 100_000.0
_FIELDS = frozenset({"location", "photo", "clip"})
_LOCATION_FIELDS = frozenset({"lat", "lon", "accuracy_m"})


class TapError(Exception):
    """A tap was not taken. The text is fixed and repeats nothing the tap held.
    `status` is the HTTP status the route answers with, and `retry_after` the
    seconds after which the same tap may be sent again, when waiting helps."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Tap:
    """A tap that was taken."""

    id: str
    received_at: datetime
    # Which store its row went to. In `mock`, whatever Beacon answers the tap with
    # is drawn from the invented scenario, and is to be labeled so.
    mode: Mode
    photo: bool
    clip: bool
    # Until when its photo or clip is kept. None for a tap that sent neither.
    media_kept_until: datetime | None


class Taps:
    """Takes taps: checks them, keeps their media, and stores their rows."""

    def __init__(
        self,
        store: EvidenceStore,
        media: TapMedia,
        *,
        clock: Callable[[], datetime] | None = None,
        bounds: Bounds = SAN_FRANCISCO,
    ) -> None:
        self._store = store
        self._media = media
        self._clock = (lambda: datetime.now(UTC)) if clock is None else clock
        self._bounds = bounds
        # When the taps of the last minute were taken, by a clock that only goes forward.
        self._recent: deque[float] = deque()
        self._counting = threading.Lock()
        # How many taps are being received just now.
        self._receiving = 0

    @property
    def store(self) -> EvidenceStore:
        return self._store

    @contextlib.contextmanager
    def receiving(self) -> Iterator[None]:
        """Hold one of the `MAX_TAPS_AT_ONCE` places for as long as a tap is being
        read from the network and taken. The route enters this before it reads a
        tap's body.

        Raises TapError when every place is held."""
        with self._counting:
            full = self._receiving >= MAX_TAPS_AT_ONCE
            if not full:
                self._receiving += 1
        if full:
            raise_bare(TapError(503, "Beacon is busy with other taps. Try again in a moment.", 1))
        try:
            yield
        finally:
            with self._counting:
                self._receiving -= 1

    def take(self, payload: Any) -> Tap:
        """Take one tap, as the JSON a phone sent:

            {"location": {"lat": 37.7763, "lon": -122.4244, "accuracy_m": 12},
             "photo": "<a JPEG file, in base64>", "clip": "<a WAV file, in base64>"}

        `accuracy_m`, `photo`, and `clip` are optional. Its row is stored and its
        media kept, or neither is.

        Raises TapError when the tap cannot be used or cannot be taken just now."""
        point, accuracy_m, photo, clip = _read(payload)
        if not self._bounds.holds(point):
            raise_bare(
                TapError(422, "Beacon covers San Francisco, and this tap is from somewhere else.")
            )
        self._count()
        now = self._clock()
        tap_id = secrets.token_urlsafe(12)
        kept_until = None
        try:
            for kind, cleaned in (("photo", photo), ("clip", clip)):
                if cleaned is not None:
                    kept_until = self._media.keep(tap_id, kind, cleaned.content, now)  # type: ignore[arg-type]
            self._store.add([_row(tap_id, now, point, accuracy_m, photo, clip, kept_until)])
        except BaseException as error:
            # A tap is taken whole or not at all: no photo without its row.
            self._media.drop(tap_id)
            refusal = _refusal(error)
            if refusal is None:
                raise
        else:
            return Tap(
                id=tap_id,
                received_at=now,
                mode=self._store.mode,
                photo=photo is not None,
                clip=clip is not None,
                media_kept_until=kept_until,
            )
        raise_bare(refusal)

    def media(self, tap_id: str, kind: MediaKind) -> bytes | None:
        """A tap's photo (a JPEG file) or clip (a WAV file), as it was cleaned, or
        None when the tap sent none, its hour is over, or there is no such tap."""
        if not is_tap_id(tap_id):
            return None
        return self._media.read(tap_id, kind, self._clock())

    def sweep(self) -> int:
        """Delete the media whose hour is over, and return how many files that was."""
        return self._media.sweep(self._clock())

    def close(self) -> None:
        """Take no more taps: the store is closed."""
        self._store.close()

    def _count(self) -> None:
        """Count this tap against the limit for a minute, or refuse it."""
        with self._counting:
            now = time.monotonic()
            while self._recent and now - self._recent[0] >= 60.0:
                self._recent.popleft()
            if len(self._recent) >= MAX_TAPS_PER_MINUTE:
                wait = math.ceil(60.0 - (now - self._recent[0]))
                raise_bare(
                    TapError(
                        429,
                        "Beacon is taking as many taps as it can just now. Try again in a minute.",
                        retry_after=max(1, wait),
                    )
                )
            self._recent.append(now)


def _refusal(error: BaseException) -> TapError | None:
    """What a tap is refused with after a failure to keep it, or None for a failure
    that is not one of keeping it."""
    if isinstance(error, MediaFullError):
        return TapError(
            503,
            "Beacon cannot keep another photo or clip just now. Send the tap without one.",
            retry_after=600,
        )
    # The store is closed, or another writer held its file for too long, or the disk
    # would not take the file.
    if isinstance(error, EvidenceStoreError | sqlite3.Error | OSError):
        return TapError(503, "Beacon could not keep this tap. Try again in a moment.", 5)
    return None


def _row(
    tap_id: str,
    now: datetime,
    point: Point,
    accuracy_m: int | None,
    photo: Photo | None,
    clip: Clip | None,
    kept_until: datetime | None,
) -> NewEvidence:
    return NewEvidence(
        kind="tap",
        source=SOURCE,
        ref=tap_id,
        occurred_at=now,
        # Somebody noticed something. What, nobody has looked into yet.
        confidence="low",
        # A real person's tap, whichever store it is kept in.
        origin="live",
        point=Point(round(point.lat, PLACES), round(point.lon, PLACES)),
        data={
            # How far off the phone said its position could be, in metres. The
            # position kept is the middle of a box about a block across.
            "accuracy_m": accuracy_m,
            "photo": None
            if photo is None
            else {"bytes": len(photo.content), "width": photo.width, "height": photo.height},
            "clip": None if clip is None else {"bytes": len(clip.content), "seconds": clip.seconds},
            "media_kept_until": None if kept_until is None else kept_until.isoformat(),
        },
    )


def _read(payload: Any) -> tuple[Point, int | None, Photo | None, Clip | None]:
    """A tap's place and its cleaned media. Raises TapError for what is not a tap."""
    location = payload.get("location") if isinstance(payload, Mapping) else None
    shaped = (
        isinstance(location, Mapping)
        and payload.keys() <= _FIELDS
        and location.keys() <= _LOCATION_FIELDS
    )
    point = _point(location) if shaped else None
    if point is None:
        raise_bare(
            TapError(
                422,
                "A tap is a JSON object with a location, which has lat and lon in degrees, "
                "and optionally accuracy_m, a photo, and a clip.",
            )
        )
    accuracy = location.get("accuracy_m")
    if accuracy is not None and not (_is_number(accuracy) and 0 <= accuracy <= _MAX_ACCURACY_M):
        raise_bare(TapError(422, "A tap's accuracy_m is a number of metres, when it is given."))
    photo = _media(payload.get("photo"), MAX_PHOTO_BYTES, clean_photo)
    clip = _media(payload.get("clip"), MAX_CLIP_BYTES, clean_clip)
    return point, None if accuracy is None else math.ceil(accuracy), photo, clip


def _point(location: Mapping[str, Any]) -> Point | None:
    lat, lon = location.get("lat"), location.get("lon")
    if not (_is_number(lat) and _is_number(lon)):
        return None
    try:
        return Point(float(lat), float(lon))
    except ValueError:
        pass
    return None


def _media[Cleaned](sent: Any, most: int, clean: Callable[[bytes], Cleaned]) -> Cleaned | None:
    """A tap's photo or clip, decoded and cleaned, or None when none was sent.
    Raises TapError when what was sent is not base64 or not media Beacon takes."""
    if sent is None:
        return None
    content = None
    # Four characters of base64 for three bytes, so a longer text is a larger file.
    if isinstance(sent, str) and len(sent) <= most * 4 // 3 + 4:
        try:
            content = base64.b64decode(sent, validate=True)
        except (binascii.Error, ValueError):
            content = None
    if content is None:
        raise_bare(
            TapError(422, "A tap's photo and clip are files in base64, of a size Beacon takes.")
        )
    try:
        return clean(content)
    except MediaError as error:
        message = str(error)
    # Outside the handler, whose error is raised about the file.
    raise_bare(TapError(422, message))


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
