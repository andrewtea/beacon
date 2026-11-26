"""Taps: what a person sends when they press "What's that?", and what is kept of it.

    from beacon.taps import open_taps

    taps = open_taps(os.environ)
    tap = taps.take({"location": {"lat": 37.7763, "lon": -122.4244}})
    taps.media(tap.id, "clip")   # the WAV file it sent, for an hour, else None

See `desk` for what a tap's evidence row holds and how little of a person it keeps,
and `media` for what is taken as a photo or a clip and what is stripped from it.
"""

import logging
from collections.abc import Mapping

from beacon.ember.client import default_data_dir
from beacon.ingest.setup import open_store
from beacon.radio.audio import Ffmpeg
from beacon.taps.desk import (
    MAX_TAPS_AT_ONCE,
    MAX_TAPS_PER_MINUTE,
    PLACES,
    SOURCE,
    Tap,
    TapError,
    Taps,
)
from beacon.taps.media import (
    KEPT,
    MAX_CLIP_BYTES,
    MAX_CLIP_SECONDS,
    MAX_PHOTO_BYTES,
    MAX_PHOTO_SIDE,
    Clip,
    MediaError,
    MediaKind,
    Photo,
    PhotoConverter,
    Swept,
    TapMedia,
    clean_clip,
    clean_photo,
)

__all__ = [
    "KEPT",
    "MAX_CLIP_BYTES",
    "MAX_CLIP_SECONDS",
    "MAX_PHOTO_BYTES",
    "MAX_PHOTO_SIDE",
    "MAX_TAPS_AT_ONCE",
    "MAX_TAPS_PER_MINUTE",
    "PLACES",
    "SOURCE",
    "Clip",
    "MediaError",
    "MediaKind",
    "Photo",
    "PhotoConverter",
    "Swept",
    "Tap",
    "TapError",
    "TapMedia",
    "Taps",
    "clean_clip",
    "clean_photo",
    "open_taps",
]

logger = logging.getLogger(__name__)


def open_taps(env: Mapping[str, str]) -> Taps:
    """The taps of this deployment: their rows go to the evidence store the
    environment picks (`beacon.ingest.open_store`), and their media is kept beside
    it under the data directory, apart for the mock store and the live one.

    A photo is drawn again by ffmpeg, confined by bubblewrap, which needs Linux.
    Where that cannot be run, the log says so, and a tap that sends a photo is
    refused: a photo is not kept as it was sent.

    Raises what `open_store` raises."""
    data = default_data_dir(env)
    # ffmpeg is shown the system's programs and nothing of Beacon's.
    photos: Ffmpeg | None = Ffmpeg(hidden=[data])
    if photos is not None and photos.problem() is not None:
        logger.warning(
            "Taps are taken without photos: a photo is drawn again by ffmpeg, confined "
            "by bubblewrap (bwrap), on Linux, and that cannot be run here."
        )
        photos = None
    store = open_store(env)
    return Taps(store, TapMedia(data / "taps", store.mode), photos=photos)
