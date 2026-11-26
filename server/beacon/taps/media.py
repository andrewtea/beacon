"""A tap's photo and clip: what is taken, what is kept of it, and for how long.

A photo is from somebody's camera and a clip from their microphone, so both can show
or record people, and a file from a phone carries more than its picture: where and
when it was taken, on what device, and sometimes a second picture or a video behind
the first. So:

- **Only the picture and only the sound are kept.** A photo is a JPEG, and what is
  kept is a new JPEG that Beacon had drawn from it (`clean_photo`): the file is cut
  down to what a decoder needs, decoded by a confined ffmpeg, made no larger than
  `MAX_PHOTO_SIDE` on a side, and encoded again. Nothing of the file that was sent
  is copied into the one that is kept, so no Exif, no GPS position, no thumbnail,
  no comment, and no bytes tucked where a decoder does not look. A file that cannot
  be decoded is not a photo. A clip is a WAV file of plain samples, and is written
  out again as its samples and nothing else (`clean_clip`). Another kind of file is
  refused; the app makes these two.
- **They are kept for an hour.** `TapMedia` holds the files under the data
  directory, readable by Beacon's own user only, and a file older than `KEPT` is
  deleted and never handed out. An hour is long enough for the tap to be answered
  and looked at, and what was learned from the media outlives it in whatever the
  investigator writes down.
- **They are kept in bounds.** All the media together, of the mock store's taps and
  the live store's, is at most `MAX_STORED_BYTES`; past that a tap with media is
  refused until some has expired. When what is stored cannot be counted, nothing
  more is kept.
- **A file belongs to a row.** A file is written, and synced to the disk, before its
  tap's row is stored. If Beacon dies in between, the file is there with no row:
  `beacon.taps.desk` hands out no file without its row, and the sweep deletes one
  that has had no row for `ORPHANED`.

What the new picture loses besides: the note of which way up it is, so the app sends
a picture it has already turned upright, and the colour profile, without the colours
being converted for the lack of one. A picture in a wider gamut than sRGB comes out a
little flat. The app draws its picture to a canvas before it sends it, which makes
it upright and sRGB.

Nothing here reads a picture or hears a clip. That is the investigator's to do, with
Ember, and only as evidence for the tap it came with.
"""

import io
import os
import re
import struct
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, get_args

from beacon.errors import raise_bare
from beacon.evidence import Mode

MediaKind = Literal["photo", "clip"]

MAX_PHOTO_BYTES = 4_000_000
# The most pixels a photo may say it has, which is what decoding it is bounded by,
# and the longest side of the picture that is kept: enough to tell smoke from fog.
MAX_PHOTO_PIXELS = 50_000_000
MAX_PHOTO_SIDE = 2048
# The app records five seconds. A clip is refused past this.
MAX_CLIP_SECONDS = 15.0
MAX_CLIP_BYTES = 3_000_000
# How long a photo or a clip is kept after its tap.
KEPT = timedelta(hours=1)
# The most that all taps' media may take on disk.
MAX_STORED_BYTES = 500_000_000
# How long a file may be there with no row before the sweep deletes it. Longer than
# a tap takes to be stored, so a file whose row is on its way is left alone.
ORPHANED = timedelta(minutes=5)

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


class PhotoConverter(Protocol):
    """Decodes a JPEG and draws it again as a new one. `beacon.radio.audio.Ffmpeg`
    is the one there is."""

    def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
        """A new JPEG of the picture, or None when it cannot be decoded.

        Raises `beacon.radio.ConversionStopped` when the converter is closed."""
        ...


@dataclass(frozen=True)
class Swept:
    """What a sweep did."""

    deleted: int
    # Files that should have been deleted and could not be. They are still not
    # handed out, and the next sweep tries again.
    failed: int = 0


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


def clean_photo(photo: bytes, converter: PhotoConverter) -> Photo:
    """The picture of a JPEG file, as a new JPEG file that holds nothing else.

    The file is first cut down to what a decoder needs (`_clean_jpeg`), so that the
    decoder is never shown its Exif, its thumbnail, or a second picture behind the
    first. Then `converter` decodes that and encodes the picture again, and it is
    the new file that is kept, cut down the same way once more. A file is only
    decoded if it says it has no more than `MAX_PHOTO_PIXELS`.

    Raises MediaError when it is not a JPEG that can be decoded, or is larger than
    a photo may be, and what `converter` raises when it is closed."""
    drawn = None
    sent = None if len(photo) > MAX_PHOTO_BYTES else _clean_jpeg(photo)
    if sent is not None and sent.width * sent.height <= MAX_PHOTO_PIXELS:
        made = converter.to_jpeg(sent.content, max_side=MAX_PHOTO_SIDE, max_bytes=MAX_PHOTO_BYTES)
        if made is not None and len(made) <= MAX_PHOTO_BYTES:
            drawn = _clean_jpeg(made)
    if drawn is None:
        raise_bare(MediaError("A tap's photo is a JPEG file of at most four megabytes."))
    return drawn


def _clean_jpeg(photo: bytes) -> Photo | None:
    """A JPEG with only the segments a decoder needs (`_NEEDED`), the JFIF header
    without its thumbnail, and Adobe's note of how the colours are stored, and with
    nothing after the end of the image. None for what is not laid out as a JPEG
    with one frame is. The picture's data is copied here, not decoded."""
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
            # One frame: a file with several can say one size and decode to another.
            if size is not None or len(payload) < 5:
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
    """The photos and clips of recent taps, as files under one directory: `root`,
    with a directory in it for the taps of each store. Files are kept and read for
    the store of `mode` only. They are counted and swept for both, so that media
    kept while Beacon ran on the other store neither outstays its hour nor has an
    allowance of its own."""

    def __init__(self, root: Path, mode: Mode) -> None:
        if mode not in get_args(Mode):
            raise ValueError(f"mode must be one of {get_args(Mode)}.")
        self._directory = root / mode
        self._directories = tuple(root / each for each in get_args(Mode))
        # One file is checked against the limit and written at a time, and no
        # sweep runs meanwhile.
        self._writing = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._directory

    def keep(self, tap_id: str, kind: MediaKind, content: bytes, now: datetime) -> datetime:
        """Keep a tap's photo or clip, already cleaned, and return when it will be
        deleted. The file is whole or absent: it is written under another name,
        synced to the disk, and then renamed.

        Raises MediaFullError when all the media together would pass
        `MAX_STORED_BYTES`, after what has expired was deleted, and OSError when the
        file cannot be written or what is stored cannot be counted."""
        path = self._path(tap_id, kind)
        with self._writing:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._stored_bytes() + len(content) > MAX_STORED_BYTES:
                self._sweep(now, None)
                if self._stored_bytes() + len(content) > MAX_STORED_BYTES:
                    raise_bare(MediaFullError("No more photos or clips can be kept just now."))
            part = path.with_name(f".{path.name}.part")
            descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(content)
                    file.flush()
                    # On the disk before the row that names it is.
                    os.fsync(file.fileno())
                # Its age is counted from the tap, by the clock the caller keeps.
                os.utime(part, (now.timestamp(), now.timestamp()))
                os.replace(part, path)
                _sync_directory(self._directory)
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
            # Not handed out, whatever went wrong. The sweep reports what stays.
            return None

    def drop(self, tap_id: str) -> None:
        """Delete what is kept of a tap. Raises OSError when a file that is there
        cannot be deleted, after trying each."""
        failure: OSError | None = None
        for kind in _SUFFIX:
            try:
                self._path(tap_id, kind).unlink(missing_ok=True)  # type: ignore[arg-type]
            except OSError as error:
                failure = error
        if failure is not None:
            raise failure

    def sweep(
        self, now: datetime, with_rows: Callable[[list[str]], set[str]] | None = None
    ) -> Swept:
        """Delete every file whose hour is over, in both stores' directories, and
        every file that was being written when Beacon stopped. With `with_rows`,
        which says which of some taps of this store have their rows, a file of this
        store's that has been there for `ORPHANED` with no row is deleted as well:
        its tap was never taken.

        Raises OSError when a directory that is there cannot be listed."""
        # Not while a file is being written, whose unfinished copy this would take.
        with self._writing:
            return self._sweep(now, with_rows)

    def _sweep(self, now: datetime, with_rows: Callable[[list[str]], set[str]] | None) -> Swept:
        failed = 0
        over: list[Path] = []
        # The files that are old enough to be missed, by the tap each says it is of.
        unsure: dict[Path, str] = {}
        for path in self._files():
            try:
                written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
                continue
            age = now - written
            if age >= KEPT or path.name.endswith(".part"):
                over.append(path)
            elif with_rows is not None and path.parent == self._directory and age >= ORPHANED:
                unsure[path] = path.name.split(".", 1)[0]
        if unsure and with_rows is not None:
            stored = with_rows(sorted(set(unsure.values())))
            over += [path for path, tap_id in unsure.items() if tap_id not in stored]
        deleted = 0
        for path in over:
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
        return Swept(deleted, failed)

    def _stored_bytes(self) -> int:
        """What all the media takes, in both stores' directories. Raises OSError
        when it cannot be counted: a file that is gone counts for nothing, and one
        that cannot be measured is not taken to be empty."""
        total = 0
        for path in self._files():
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def _files(self) -> list[Path]:
        """Every file in both stores' directories. Raises OSError when a directory
        that is there cannot be listed."""
        files: list[Path] = []
        for directory in self._directories:
            try:
                files += [path for path in directory.iterdir() if path.is_file()]
            except FileNotFoundError:
                continue
        return files

    def _path(self, tap_id: str, kind: MediaKind) -> Path:
        if not is_tap_id(tap_id) or kind not in _SUFFIX:
            # An id is part of a file's name, so one that is not shaped like an id
            # is never made into a path.
            raise ValueError("That is not a tap's id and a kind of media.")
        return self._directory / f"{tap_id}.{_SUFFIX[kind]}"


def _sync_directory(directory: Path) -> None:
    """Have the disk hold a directory's new entry, where the system can say so."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
