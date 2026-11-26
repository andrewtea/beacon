"""A tap's photo and clip: what is taken, what is kept of it, and for how long.

A photo is from somebody's camera and a clip from their microphone, so both can show
or record people, and a file from a phone carries more than its picture: where and
when it was taken, on what device, and sometimes a second picture or a video behind
the first. So:

- **Only the picture and only the sound are kept.** A photo is a JPEG, and is written
  out again with nothing but what a decoder needs (`clean_photo`): no Exif, no GPS
  position, no thumbnail, no comment, nothing after the end of the image. A clip is
  a WAV file of plain samples, and is written out again as its samples and nothing
  else (`clean_clip`). Another kind of file is refused; the app makes these two.
- **They are kept for an hour.** `TapMedia` holds the files under the data
  directory, readable by Beacon's own user only, and a file older than `KEPT` is
  deleted and never handed out. An hour is long enough for the tap to be answered
  and looked at, and what was learned from the media outlives it in whatever the
  investigator writes down.
- **They are kept in bounds.** All the media together is at most
  `MAX_STORED_BYTES`; past that a tap with media is refused until some has expired.

Nothing here reads a picture or hears a clip. That is the investigator's to do, with
Ember, and only as evidence for the tap it came with.
"""

import io
import os
import re
import struct
import threading
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from beacon.errors import raise_bare

MediaKind = Literal["photo", "clip"]

MAX_PHOTO_BYTES = 4_000_000
# The app records five seconds. A clip is refused past this.
MAX_CLIP_SECONDS = 15.0
MAX_CLIP_BYTES = 3_000_000
# How long a photo or a clip is kept after its tap.
KEPT = timedelta(hours=1)
# The most that all taps' media may take on disk.
MAX_STORED_BYTES = 500_000_000

_SUFFIX: dict[str, str] = {"photo": "photo.jpg", "clip": "clip.wav"}
_TAP_ID = re.compile(r"[A-Za-z0-9_-]{8,40}")
# What Python's `wave` raises on a file that is not what it reads, which for bytes
# from the internet is more than its own error.
_NOT_WAVE = (wave.Error, EOFError, struct.error, ValueError, RuntimeError, ArithmeticError)

_SOI, _EOI, _SOS = 0xD8, 0xD9, 0xDA
# The markers that begin a frame, which say how large the picture is.
_FRAMES = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# The segments a decoder needs, which are copied as they are: a frame, a scan's
# header, the tables (Huffman, arithmetic, quantization), the restart interval, and
# the number of lines. Every other segment is dropped, whatever it is.
_NEEDED = _FRAMES | {0xC4, 0xCC, 0xDB, 0xDD, _SOS, 0xDC}
# The markers that stand alone, with no length after them.
_BARE = frozenset({0x01, *range(0xD0, 0xD8)})
_MAX_SIDE = 16_000


class MediaError(Exception):
    """A photo or a clip is not one Beacon takes. The text is fixed and says nothing
    of the file."""


class MediaFullError(Exception):
    """No more media can be kept until some has expired. The text is fixed."""


@dataclass(frozen=True)
class Photo:
    """A photo as it is kept: a JPEG with nothing in it but the picture."""

    content: bytes
    width: int
    height: int


@dataclass(frozen=True)
class Clip:
    """A clip as it is kept: a WAV file with nothing in it but its samples."""

    content: bytes
    seconds: float


def clean_photo(photo: bytes) -> Photo:
    """A JPEG written out again with only what a decoder needs to show it: its
    frames, tables, and scans (`_NEEDED`), the JFIF header without its thumbnail,
    and Adobe's note of how the colours are stored. Every other segment is dropped,
    and so is anything after the end of the image. Exif goes with them, and with it
    the camera, the time, and the GPS position; so does the note of which way up
    the picture is, so the app sends a picture it has already turned upright. The
    picture's own data is copied as it is, not decoded.

    Raises MediaError when it is not a JPEG that can be read to its end, or is
    larger than a photo may be."""
    cleaned = None if len(photo) > MAX_PHOTO_BYTES else _clean_jpeg(photo)
    if cleaned is None:
        raise_bare(MediaError("A tap's photo is a JPEG file of at most four megabytes."))
    return cleaned


def _clean_jpeg(photo: bytes) -> Photo | None:
    if photo[:2] != bytes([0xFF, _SOI]):
        return None
    out = bytearray(photo[:2])
    at, end = 2, len(photo)
    size: tuple[int, int] | None = None
    scanned = False
    while at < end:
        if photo[at] != 0xFF:
            return None
        while at < end and photo[at] == 0xFF:
            # A marker may have fill bytes in front of it.
            at += 1
        if at >= end:
            return None
        marker = photo[at]
        at += 1
        if marker == _EOI:
            if not scanned or size is None:
                return None
            # Whatever follows the end of the image is not the image.
            return Photo(bytes(out) + bytes([0xFF, _EOI]), width=size[0], height=size[1])
        if marker in _BARE or marker in (0x00, _SOI):
            return None
        length = int.from_bytes(photo[at : at + 2], "big")
        if length < 2 or at + length > end:
            return None
        payload = photo[at + 2 : at + length]
        at += length
        kept = _kept_segment(marker, payload)
        if kept is not None:
            out += bytes([0xFF, marker]) + (len(kept) + 2).to_bytes(2, "big") + kept
        if marker in _FRAMES:
            if len(payload) < 5:
                return None
            height, width = (int.from_bytes(payload[n : n + 2], "big") for n in (1, 3))
            if not (0 < width <= _MAX_SIDE and 0 < height <= _MAX_SIDE):
                return None
            size = (width, height)
        if marker == _SOS:
            # The picture's data follows, up to the next marker that is not part of it.
            scanned = True
            start = at
            while True:
                at = photo.find(b"\xff", at)
                if at < 0 or at + 1 >= end:
                    return None
                following = photo[at + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    at += 2
                elif following == 0xFF:
                    at += 1
                else:
                    break
            out += photo[start:at]
    return None


def _kept_segment(marker: int, payload: bytes) -> bytes | None:
    """What is kept of a segment, or None for one that is dropped."""
    if marker in _NEEDED:
        return payload
    if marker == 0xE0 and payload[:5] == b"JFIF\x00" and len(payload) >= 14:
        # The header, saying that it has no thumbnail.
        return payload[:12] + b"\x00\x00"
    if marker == 0xEE and payload[:5] == b"Adobe" and len(payload) == 12:
        return payload
    return None


def clean_clip(clip: bytes) -> Clip:
    """A WAV file of plain samples written out again as its samples and nothing
    else: no chunk of text, and no more samples than are really there.

    Raises MediaError when it is not such a file, holds no samples, or plays for
    longer than a clip may."""
    frames = b""
    seconds = 0.0
    if len(clip) <= MAX_CLIP_BYTES:
        try:
            with wave.open(io.BytesIO(clip)) as read:
                channels, width, rate = (
                    read.getnchannels(),
                    read.getsampwidth(),
                    read.getframerate(),
                )
                if 1 <= channels <= 2 and 1 <= width <= 2 and 8_000 <= rate <= 48_000:
                    # One frame past the longest clip, so a longer one is seen to be.
                    frames = read.readframes(int(rate * MAX_CLIP_SECONDS) + 1)
                    frames = frames[: len(frames) - len(frames) % (channels * width)]
                    seconds = len(frames) / (channels * width) / rate
        except _NOT_WAVE:
            frames = b""
    if not frames or seconds > MAX_CLIP_SECONDS:
        # Raised outside the handler, which holds the file.
        raise_bare(
            MediaError("A tap's clip is a WAV file of plain samples, at most fifteen seconds long.")
        )
    out = io.BytesIO()
    with wave.open(out, "wb") as written:
        written.setnchannels(channels)
        written.setsampwidth(width)
        written.setframerate(rate)
        written.writeframes(frames)
    return Clip(out.getvalue(), seconds=round(seconds, 2))


def is_tap_id(value: object) -> bool:
    """Whether this is shaped like a tap's id, which is also part of a file's name."""
    return isinstance(value, str) and _TAP_ID.fullmatch(value) is not None


class TapMedia:
    """The photos and clips of recent taps, as files in one directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        # One file is checked against the limit and written at a time.
        self._writing = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._directory

    def keep(self, tap_id: str, kind: MediaKind, content: bytes, now: datetime) -> datetime:
        """Keep a tap's photo or clip, already cleaned, and return when it will be
        deleted. The file is whole or absent: it is written under another name and
        then renamed.

        Raises MediaFullError when all the media together would pass
        `MAX_STORED_BYTES`, after what has expired was deleted."""
        path = self._path(tap_id, kind)
        with self._writing:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._stored_bytes() + len(content) > MAX_STORED_BYTES:
                self._sweep(now)
                if self._stored_bytes() + len(content) > MAX_STORED_BYTES:
                    raise_bare(MediaFullError("No more photos or clips can be kept just now."))
            part = path.with_name(f".{path.name}.part")
            descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(content)
                # Its age is counted from the tap, by the clock the caller keeps.
                os.utime(part, (now.timestamp(), now.timestamp()))
                os.replace(part, path)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        return now + KEPT

    def read(self, tap_id: str, kind: MediaKind, now: datetime) -> bytes | None:
        """A tap's photo or clip, or None when it sent none or its hour is over. A
        file whose hour is over is deleted here, if the sweep has not got to it."""
        path = self._path(tap_id, kind)
        try:
            written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if now - written >= KEPT:
                path.unlink(missing_ok=True)
                return None
            return path.read_bytes()
        except OSError:
            return None

    def drop(self, tap_id: str) -> None:
        """Delete what is kept of a tap."""
        for kind in _SUFFIX:
            self._path(tap_id, kind).unlink(missing_ok=True)  # type: ignore[arg-type]

    def sweep(self, now: datetime) -> int:
        """Delete every file whose hour is over, and return how many there were. A
        file that was being written when Beacon stopped is deleted with them."""
        # Not while a file is being written, whose unfinished copy this would take.
        with self._writing:
            return self._sweep(now)

    def _sweep(self, now: datetime) -> int:
        deleted = 0
        for path in self._files():
            try:
                written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                if now - written >= KEPT or path.name.endswith(".part"):
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    def _stored_bytes(self) -> int:
        total = 0
        for path in self._files():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _files(self) -> list[Path]:
        try:
            return [path for path in self._directory.iterdir() if path.is_file()]
        except OSError:
            return []

    def _path(self, tap_id: str, kind: MediaKind) -> Path:
        if not is_tap_id(tap_id) or kind not in _SUFFIX:
            # An id is part of a file's name, so one that is not shaped like an id
            # is never made into a path.
            raise ValueError("That is not a tap's id and a kind of media.")
        return self._directory / f"{tap_id}.{_SUFFIX[kind]}"
