"""A tap's photo and clip: what is stripped from them, and how long they are kept."""

import io
import os
import sqlite3
import stat
import subprocess
import sys
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beacon.radio import ConversionStopped, Ffmpeg
from beacon.taps import media as tap_media
from beacon.taps.media import (
    KEPT,
    MAX_CLIP_BYTES,
    MAX_CLIP_SECONDS,
    MAX_PHOTO_BYTES,
    MAX_PHOTO_PIXELS,
    MAX_PHOTO_SIDE,
    ORPHANED,
    MediaError,
    MediaFullError,
    MediaLockedError,
    Swept,
    TapMedia,
    clean_clip,
    clean_photo,
)

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)

_CAN_CONVERT = sys.platform == "linux" and Ffmpeg().problem() is None
if os.environ.get("CI") and not _CAN_CONVERT:
    # CI installs ffmpeg and bubblewrap, so that what is kept of a real picture is
    # not quietly left untested there.
    raise RuntimeError("CI runs the photo tests, which need ffmpeg and a working bwrap on Linux.")
needs_ffmpeg = pytest.mark.skipif(
    not _CAN_CONVERT, reason="Drawing a photo again needs ffmpeg and bubblewrap, on Linux."
)


class Same:
    """Stands in for the converter where a test is about the file's layout: it
    hands back what it was given, as if the picture had been drawn again exactly.
    `given` is what it was shown."""

    def __init__(self) -> None:
        self.given: list[bytes] = []

    def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
        self.given.append(photo)
        return photo


SAME = Same()
TAP = "tap-AAAAAAAAAAAA"
OTHER = "tap-BBBBBBBBBBBB"

# What a phone's file says besides its picture. All of it is invented.
SECRETS = (b"GPSLatitude 37.7763", b"Jane Doe's iPhone", b"taken at home", b"a second picture")


def kept(directory: Path) -> list[str]:
    """Every file under a directory but the lock a `TapMedia` holds on its own."""
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path.name != ".lock"
    )


def segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


JFIF = b"JFIF\x00\x01\x02\x01\x00\x48\x00\x48"
QUANTIZATION = segment(0xDB, b"\x00" + bytes(range(1, 65)))
FRAME = segment(
    0xC0, b"\x08" + (480).to_bytes(2, "big") + (640).to_bytes(2, "big") + b"\x01\x01\x11\x00"
)
HUFFMAN = segment(0xC4, b"\x00" + bytes(16))
SCAN = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
# Entropy-coded data, with a stuffed FF and a restart marker in it as real data has.
PICTURE = b"\x12\x34\xff\x00\x56\xff\xd0\x78\x9a"
END = b"\xff\xd9"


def jpeg(*, extras: bool = True) -> bytes:
    """A file shaped as a JPEG is, with what a phone adds to one when `extras`."""
    parts = [b"\xff\xd8"]
    if extras:
        # A JFIF header with a one-pixel thumbnail, Exif, a second picture, a comment.
        parts += [
            segment(0xE0, JFIF + b"\x01\x01" + b"\xaa\xbb\xcc"),
            segment(0xE1, b"Exif\x00\x00" + SECRETS[0] + SECRETS[1]),
            segment(0xE2, b"MPF\x00" + SECRETS[3]),
            segment(0xFE, SECRETS[2]),
        ]
    else:
        parts += [segment(0xE0, JFIF + b"\x00\x00")]
    parts += [QUANTIZATION, FRAME, HUFFMAN, SCAN, PICTURE, END]
    if extras:
        parts += [SECRETS[3] + b" and a video after the end"]
    return b"".join(parts)


def wav(seconds: float = 1.0, *, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as written:
        written.setnchannels(channels)
        written.setsampwidth(width)
        written.setframerate(rate)
        written.writeframes(b"\x01\x02"[:width] * channels * int(rate * seconds))
    return out.getvalue()


def with_chunk(file: bytes, name: bytes, content: bytes) -> bytes:
    """A WAV file with one more chunk at its end, as a recorder adds its notes."""
    body = file[8:] + name + len(content).to_bytes(4, "little") + content
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def test_a_photo_is_kept_as_its_picture_and_nothing_a_phone_added_to_it() -> None:
    cleaned = clean_photo(jpeg(), SAME)

    # What is left is the file a camera with nothing to say would have written.
    assert cleaned.content == jpeg(extras=False)
    assert (cleaned.width, cleaned.height) == (640, 480)
    for secret in SECRETS:
        assert secret not in cleaned.content


def test_cleaning_a_photo_that_is_already_clean_changes_nothing() -> None:
    assert clean_photo(jpeg(extras=False), SAME).content == jpeg(extras=False)


@pytest.mark.parametrize("marker", [0xE3, 0xED, 0xEF, 0xF0, 0xF7, 0xC8, 0xDE, 0x02, 0xBF])
def test_a_segment_no_decoder_needs_is_dropped_whatever_it_is(marker: int) -> None:
    # Application segments, and markers reserved for uses nobody has given them.
    hidden = jpeg(extras=False).replace(QUANTIZATION, segment(marker, SECRETS[1]) + QUANTIZATION)

    assert clean_photo(hidden, SAME).content == jpeg(extras=False)


def test_a_progressive_picture_keeps_every_scan_and_loses_what_is_between_them() -> None:
    scans = SCAN + PICTURE + segment(0xE1, SECRETS[0]) + HUFFMAN + SCAN + PICTURE
    photo = jpeg(extras=False).replace(SCAN + PICTURE, scans)

    assert clean_photo(photo, SAME).content == jpeg(extras=False).replace(
        SCAN + PICTURE, SCAN + PICTURE + HUFFMAN + SCAN + PICTURE
    )


@pytest.mark.parametrize(
    "photo",
    [
        b"",
        b"\x89PNG\r\n\x1a\n" + bytes(64),
        b"GIF89a" + bytes(64),
        # Cut off in a segment, in the picture's data, and before the end marker.
        jpeg()[:40],
        jpeg(extras=False)[:-6],
        jpeg(extras=False)[:-2],
        # No frame, and no scan.
        jpeg(extras=False).replace(FRAME, b""),
        jpeg(extras=False).replace(SCAN + PICTURE, b""),
        # A frame of no size, and one larger than any camera's.
        jpeg(extras=False).replace(FRAME, segment(0xC0, b"\x08" + bytes(4) + b"\x01\x01\x11\x00")),
        jpeg(extras=False).replace(FRAME, segment(0xC0, b"\x08\xff\xff\xff\xff\x01\x01\x11\x00")),
        # A second start of image, and bytes between segments that are no marker.
        jpeg(extras=False).replace(QUANTIZATION, b"\xff\xd8" + QUANTIZATION),
        jpeg(extras=False).replace(QUANTIZATION, b"Jane Doe" + QUANTIZATION),
    ],
)
def test_what_is_not_a_whole_jpeg_is_refused_in_a_fixed_sentence(photo: bytes) -> None:
    with pytest.raises(MediaError) as refused:
        clean_photo(photo, SAME)

    assert str(refused.value) == "A tap's photo is a JPEG file of at most four megabytes."
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


def test_a_photo_larger_than_a_photo_may_be_is_refused() -> None:
    padded = jpeg(extras=False) + bytes(MAX_PHOTO_BYTES)

    with pytest.raises(MediaError, match="at most four megabytes"):
        clean_photo(padded, SAME)


def test_the_decoder_is_shown_only_what_a_decoder_needs_and_its_picture_is_what_is_kept() -> None:
    drawn = jpeg(extras=False).replace(PICTURE, b"\x21\x43\x65")

    class Draws(Same):
        def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
            super().to_jpeg(photo, max_side=max_side, max_bytes=max_bytes)
            # As an encoder that signs its work would write it.
            return drawn.replace(QUANTIZATION, segment(0xFE, b"Lavc61") + QUANTIZATION)

    converter = Draws()

    cleaned = clean_photo(jpeg(), converter)

    # The phone's Exif, thumbnail, and second picture never reach the decoder.
    assert converter.given == [jpeg(extras=False)]
    # What is kept is the new file, not the one that was sent, cut down once more.
    assert cleaned.content == drawn


@pytest.mark.parametrize(
    "made", [None, b"", b"not a picture", jpeg()[:40], bytes(MAX_PHOTO_BYTES + 1)]
)
def test_a_photo_the_decoder_makes_nothing_of_is_refused(made: bytes | None) -> None:
    class Fails:
        def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
            return made

    with pytest.raises(MediaError, match="A tap's photo is a JPEG file"):
        clean_photo(jpeg(), Fails())


def test_a_photo_that_says_it_is_larger_than_a_camera_makes_is_not_decoded() -> None:
    side = 8000
    assert side * side > MAX_PHOTO_PIXELS
    frame = segment(0xC0, b"\x08" + side.to_bytes(2, "big") * 2 + b"\x01\x01\x11\x00")
    converter = Same()

    with pytest.raises(MediaError):
        clean_photo(jpeg(extras=False).replace(FRAME, frame), converter)

    assert converter.given == []


def test_a_file_with_two_frames_is_refused_and_not_decoded() -> None:
    small = segment(0xC0, b"\x08" + (8).to_bytes(2, "big") * 2 + b"\x01\x01\x11\x00")
    converter = Same()

    # The first frame says a size that passes, and the decoder may go by the second.
    with pytest.raises(MediaError):
        clean_photo(jpeg(extras=False).replace(FRAME, small + FRAME), converter)

    assert converter.given == []


def test_a_converter_that_is_closed_says_so_and_not_that_the_photo_is_bad() -> None:
    class Closed:
        def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
            raise ConversionStopped("The converter is closed.")

    with pytest.raises(ConversionStopped):
        clean_photo(jpeg(), Closed())


def real_jpeg(tmp_path: Path, size: str = "64x48") -> bytes:
    """A picture ffmpeg draws, as a JPEG. Nothing in these tests is a photograph."""
    made = tmp_path / f"made-{size}.jpg"
    draw = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}"]
    subprocess.run([*draw, "-frames:v", "1", "-q:v", "2", str(made)], check=True)
    return made.read_bytes()


def pixels(file: bytes, tmp_path: Path) -> bytes:
    """A JPEG's picture, as grey levels, eight by six."""
    path = tmp_path / "shown.jpg"
    path.write_bytes(file)
    show = ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=8:6", "-pix_fmt", "gray"]
    return subprocess.run([*show, "-f", "rawvideo", "-"], check=True, capture_output=True).stdout


@needs_ffmpeg
def test_a_real_picture_is_drawn_again_and_nothing_of_its_file_is_kept(tmp_path: Path) -> None:
    real = real_jpeg(tmp_path)
    # Bytes where a decoder does not look: after the picture's last coded block,
    # inside the scan, and in every place a phone puts its notes.
    tucked = b"tucked into the scan"
    photo = (
        real[:2]
        + segment(0xE1, b"Exif\x00\x00" + SECRETS[0])
        + segment(0xFE, SECRETS[2])
        + real[2:-2]
        + tucked
        + real[-2:]
        + SECRETS[3]
    )

    cleaned = clean_photo(photo, Ffmpeg())

    assert (cleaned.width, cleaned.height) == (64, 48)
    for hidden in (tucked, *SECRETS, b"Lavc", b"Exif"):
        assert hidden not in cleaned.content
    # The same picture, near enough: it was encoded a second time.
    before, after = pixels(real, tmp_path), pixels(cleaned.content, tmp_path)
    assert len(before) == len(after) == 48
    assert max(abs(a - b) for a, b in zip(before, after, strict=True)) <= 12
    # What was added to the file changes nothing in what is kept of it.
    assert cleaned.content == clean_photo(real, Ffmpeg()).content


@needs_ffmpeg
def test_a_large_picture_is_kept_no_larger_than_it_needs_to_be(tmp_path: Path) -> None:
    cleaned = clean_photo(real_jpeg(tmp_path, "3000x1000"), Ffmpeg())

    assert (cleaned.width, cleaned.height) == (MAX_PHOTO_SIDE, 683)


@needs_ffmpeg
def test_a_file_laid_out_as_a_jpeg_that_holds_no_picture_is_not_a_photo(tmp_path: Path) -> None:
    real = real_jpeg(tmp_path)
    scan = real.index(b"\xff\xda")
    for file in (
        # The layout of a JPEG, and no picture in it that a decoder can find.
        jpeg(),
        # A real file's headers, and noise where its picture was.
        real[: scan + 14] + bytes(range(1, 200)) + real[-2:],
        # A real file that stops half way through its picture.
        real[: len(real) // 2] + real[-2:],
    ):
        with pytest.raises(MediaError, match="A tap's photo is a JPEG file"):
            clean_photo(file, Ffmpeg())


def test_a_clip_is_kept_as_its_samples_and_nothing_a_recorder_added_to_it() -> None:
    noted = with_chunk(wav(2.0), b"LIST", b"INFOIART\x08\x00\x00\x00Jane Doe")

    cleaned = clean_clip(noted)

    assert cleaned.content == wav(2.0)
    assert cleaned.seconds == 2.0
    assert b"Jane Doe" not in cleaned.content


def test_a_clip_that_says_it_is_longer_than_it_is_keeps_only_the_samples_it_has() -> None:
    file = bytearray(wav(1.0))
    # The data chunk's length, said to be a hundred times what follows it.
    at = file.index(b"data") + 4
    file[at : at + 4] = (100 * 32_000).to_bytes(4, "little")

    cleaned = clean_clip(bytes(file))

    assert cleaned.content == wav(1.0)
    assert cleaned.seconds == 1.0


@pytest.mark.parametrize(
    "clip",
    [
        b"",
        b"RIFF" + bytes(40),
        b"OggS" + bytes(64),
        # No samples at all.
        wav(0.0),
        # A second longer than a clip may be.
        wav(MAX_CLIP_SECONDS + 1.0, rate=8_000),
        # Sample rates and widths that are not a phone microphone's.
        wav(1.0, rate=4_000),
        wav(0.2, rate=96_000),
        wav(0.5, width=3),
        wav(0.5, channels=3),
    ],
)
def test_what_is_not_a_short_plain_wav_is_refused_in_a_fixed_sentence(clip: bytes) -> None:
    with pytest.raises(MediaError) as refused:
        clean_clip(clip)

    assert str(refused.value) == (
        "A tap's clip is a WAV file of plain samples, at most fifteen seconds long."
    )
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


def test_a_clip_of_exactly_the_longest_length_is_taken_and_a_larger_file_is_not() -> None:
    assert clean_clip(wav(MAX_CLIP_SECONDS, rate=8_000)).seconds == MAX_CLIP_SECONDS

    with pytest.raises(MediaError):
        clean_clip(wav(1.0) + bytes(MAX_CLIP_BYTES))


def test_media_is_kept_for_an_hour_in_files_only_beacon_can_read(tmp_path: Path) -> None:
    media = TapMedia(tmp_path / "taps", "mock")

    kept_until = media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(TAP, "clip", b"a sound", NOON)

    assert kept_until == NOON + KEPT
    assert media.read(TAP, "photo", NOON + timedelta(minutes=59)) == b"a picture"
    assert media.read(TAP, "clip", NOON + timedelta(minutes=59)) == b"a sound"
    assert media.read(OTHER, "photo", NOON) is None
    files = sorted((tmp_path / "taps" / "mock").iterdir())
    assert [file.name for file in files] == [f"{TAP}.clip.wav", f"{TAP}.photo.jpg"]
    assert {stat.S_IMODE(file.stat().st_mode) for file in files} == {0o600}
    assert stat.S_IMODE((tmp_path / "taps" / "mock").stat().st_mode) == 0o700


def test_media_whose_hour_is_over_is_not_handed_out_and_is_deleted(tmp_path: Path) -> None:
    media = TapMedia(tmp_path, "mock")
    media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(OTHER, "clip", b"a sound", NOON + timedelta(minutes=30))

    # Asked for at the end of its hour, before any sweep has got to it.
    assert media.read(TAP, "photo", NOON + KEPT) is None
    assert [file.name for file in (tmp_path / "mock").iterdir()] == [f"{OTHER}.clip.wav"]

    assert media.sweep(NOON + KEPT) == Swept(deleted=0)
    assert media.sweep(NOON + KEPT + timedelta(minutes=30)) == Swept(deleted=1)
    assert list((tmp_path / "mock").iterdir()) == []


def test_a_sweep_deletes_what_a_stopped_server_left_half_written(tmp_path: Path) -> None:
    media = TapMedia(tmp_path, "mock")
    media.keep(TAP, "photo", b"a picture", NOON)
    (tmp_path / "mock" / f".{OTHER}.photo.jpg.part").write_bytes(b"half a pic")

    assert media.sweep(NOON) == Swept(deleted=1)

    assert [file.name for file in (tmp_path / "mock").iterdir()] == [f"{TAP}.photo.jpg"]


def test_a_file_with_no_row_is_deleted_once_it_has_been_missed_for_long_enough(
    tmp_path: Path,
) -> None:
    media = TapMedia(tmp_path, "mock")
    # Two taps' files, of which one tap's row was never stored: Beacon died
    # between the file and the row.
    media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(OTHER, "photo", b"a picture", NOON)
    asked: list[list[str]] = []

    def with_rows(tap_ids: list[str]) -> set[str]:
        asked.append(tap_ids)
        return {TAP}

    # A file this new can be one whose row is on its way. Nobody is asked about it.
    soon = NOON + ORPHANED - timedelta(seconds=1)
    assert media.sweep(soon, with_rows) == Swept(deleted=0)
    assert asked == []

    assert media.sweep(NOON + ORPHANED, with_rows) == Swept(deleted=1)

    assert asked == [[TAP, OTHER]]
    assert [file.name for file in (tmp_path / "mock").iterdir()] == [f"{TAP}.photo.jpg"]


def test_the_other_stores_media_is_swept_and_counted_and_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kept while Beacon ran on the invented scenario, by real people all the same.
    mock = TapMedia(tmp_path, "mock")
    mock.keep(TAP, "photo", b"0123456789", NOON)
    mock.close()
    live = TapMedia(tmp_path, "live")

    # It is the other store's, so it is not this one's to hand out, and nothing
    # this store knows of rows is asked of it.
    assert live.read(TAP, "photo", NOON) is None
    assert live.sweep(NOON + ORPHANED, lambda _ids: set()) == Swept(deleted=0)

    # It counts against the one allowance there is.
    monkeypatch.setattr(tap_media, "MAX_STORED_BYTES", 15)
    with pytest.raises(MediaFullError):
        live.keep(OTHER, "photo", b"0123456789", NOON + timedelta(minutes=1))

    # And it is deleted when its hour is over, though Beacon never ran on its
    # store again.
    assert live.sweep(NOON + KEPT) == Swept(deleted=1)
    assert list((tmp_path / "mock").iterdir()) == []


def test_dropping_a_tap_deletes_its_media_and_no_other_taps(tmp_path: Path) -> None:
    media = TapMedia(tmp_path, "mock")
    for tap in (TAP, OTHER):
        media.keep(tap, "photo", b"a picture", NOON)
        media.keep(tap, "clip", b"a sound", NOON)

    media.drop(TAP)

    assert sorted(file.name for file in (tmp_path / "mock").iterdir()) == [
        f"{OTHER}.clip.wav",
        f"{OTHER}.photo.jpg",
    ]


def test_all_the_media_together_stays_under_a_limit_and_expired_media_makes_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tap_media, "MAX_STORED_BYTES", 25)
    media = TapMedia(tmp_path, "mock")
    media.keep(TAP, "photo", b"0123456789", NOON)
    media.keep(TAP, "clip", b"0123456789", NOON)

    with pytest.raises(MediaFullError) as full:
        media.keep(OTHER, "photo", b"0123456789", NOON + timedelta(minutes=59))

    assert str(full.value) == "No more photos or clips can be kept just now."
    assert media.read(OTHER, "photo", NOON + timedelta(minutes=59)) is None
    # An hour after the first two, they are deleted to make room.
    media.keep(OTHER, "photo", b"0123456789", NOON + KEPT)
    assert [file.name for file in (tmp_path / "mock").iterdir()] == [f"{OTHER}.photo.jpg"]


def _refuse(method: str, failing: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Have `Path.<method>` fail for one path as it does for a disk in trouble."""
    real = getattr(Path, method)

    def fail(self: Path, *args: object, **kwargs: object) -> object:
        if self == failing:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, fail)


def test_media_that_cannot_be_counted_is_not_taken_to_be_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = TapMedia(tmp_path, "mock")
    media.keep(TAP, "photo", b"a picture", NOON)
    _refuse("iterdir", tmp_path / "mock", monkeypatch)

    # Nothing says how much is stored, so no more is kept, and no sweep claims
    # to have looked.
    with pytest.raises(PermissionError):
        media.keep(OTHER, "photo", b"a picture", NOON)
    with pytest.raises(PermissionError):
        media.sweep(NOON + KEPT)

    monkeypatch.undo()
    assert [file.name for file in (tmp_path / "mock").iterdir()] == [f"{TAP}.photo.jpg"]


def test_a_file_that_cannot_be_deleted_is_reported_and_not_handed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = TapMedia(tmp_path, "mock")
    media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(OTHER, "photo", b"a picture", NOON)
    _refuse("unlink", tmp_path / "mock" / f"{TAP}.photo.jpg", monkeypatch)

    # The other file is deleted, and this one is counted, not passed over.
    assert media.sweep(NOON + KEPT) == Swept(deleted=1, failed=1)
    assert media.read(TAP, "photo", NOON + KEPT) is None
    with pytest.raises(PermissionError):
        media.drop(TAP)


@pytest.mark.parametrize(
    "tap_id", ["", "short", "../../etc/passwd", "tap/AAAAAAAA", "a" * 41, None]
)
def test_a_name_that_is_not_shaped_as_a_taps_id_is_never_made_into_a_path(
    tmp_path: Path, tap_id: object
) -> None:
    media = TapMedia(tmp_path / "taps", "mock")

    with pytest.raises(ValueError, match="not a tap's id"):
        media.keep(tap_id, "photo", b"a picture", NOON)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a tap's id"):
        media.read(TAP, "video", NOON)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mode must be"):
        TapMedia(tmp_path / "elsewhere", "../elsewhere")  # type: ignore[arg-type]

    assert kept(tmp_path) == []


def test_a_sweep_deletes_what_is_over_though_the_store_cannot_say_which_files_have_rows(
    tmp_path: Path,
) -> None:
    media = TapMedia(tmp_path, "mock")
    media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(OTHER, "photo", b"a picture", NOON + timedelta(minutes=50))

    def broken(_tap_ids: list[str]) -> set[str]:
        raise sqlite3.OperationalError("disk I/O error")

    # An hour after the first: it is over, which needs no store to know. The
    # second may have no row, and nothing says so, so it is left and counted.
    swept = media.sweep(NOON + KEPT, broken)

    assert swept == Swept(deleted=1, failed=0, unchecked=1)
    assert kept(tmp_path) == [f"mock/{OTHER}.photo.jpg"]


def test_one_process_at_a_time_keeps_the_media_of_a_data_directory(tmp_path: Path) -> None:
    media = TapMedia(tmp_path / "taps", "mock")

    # Not a second one here, for either store, and not one in another process.
    for mode in ("mock", "live"):
        with pytest.raises(MediaLockedError, match="Another Beacon process is taking taps"):
            TapMedia(tmp_path / "taps", mode)  # type: ignore[arg-type]
    other = (
        "import sys; from pathlib import Path; from beacon.taps.media import *\n"
        "try: TapMedia(Path(sys.argv[1]), 'mock')\n"
        "except MediaLockedError: print('locked')\n"
        "else: print('taken')\n"
    )
    command = [sys.executable, "-c", other, str(tmp_path / "taps")]
    assert subprocess.run(command, capture_output=True, text=True, check=True).stdout == "locked\n"

    media.close()

    # Given up, it keeps and sweeps nothing more, and another may take it.
    with pytest.raises(OSError, match="closed"):
        media.keep(TAP, "photo", b"a picture", NOON)
    with pytest.raises(OSError, match="closed"):
        media.sweep(NOON)
    assert subprocess.run(command, capture_output=True, text=True, check=True).stdout == "taken\n"
    TapMedia(tmp_path / "taps", "live").close()


def test_a_file_is_on_the_disk_with_its_age_and_its_directories_before_it_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[tuple[str, str]] = []
    real_fsync, real_utime, real_replace = os.fsync, os.utime, os.replace

    def fsync(descriptor: int) -> None:
        done.append(("sync", os.path.relpath(os.readlink(f"/proc/self/fd/{descriptor}"), tmp_path)))
        real_fsync(descriptor)

    def utime(target: object, times: tuple[float, float]) -> None:
        done.append(("age", ""))
        real_utime(target, times)  # type: ignore[arg-type]

    def replace(source: Path, target: Path) -> None:
        done.append(("name", target.name))
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "utime", utime)
    monkeypatch.setattr(os, "replace", replace)

    media = TapMedia(tmp_path / "data" / "taps", "mock")
    media.keep(TAP, "photo", b"a picture", NOON)

    assert done == [
        # Each directory that was made is held by the one above it.
        ("sync", "."),
        ("sync", "data"),
        ("sync", "data/taps"),
        # The file's age is set before the file is synced, and both before it has
        # the name it is read by. Then the directory holds the name.
        ("age", ""),
        ("sync", f"data/taps/mock/.{TAP}.photo.jpg.part"),
        ("name", f"{TAP}.photo.jpg"),
        ("sync", "data/taps/mock"),
    ]
    # Each of them is Beacon's alone.
    for directory in ("data", "data/taps", "data/taps/mock"):
        assert stat.S_IMODE((tmp_path / directory).stat().st_mode) == 0o700


def test_a_file_is_not_read_after_the_moment_its_tap_was_told_whatever_its_own_age(
    tmp_path: Path,
) -> None:
    media = TapMedia(tmp_path, "mock")
    kept_until = media.keep(TAP, "photo", b"a picture", NOON)
    # Restored from a copy an hour later, which made the file new again.
    path = tmp_path / "mock" / f"{TAP}.photo.jpg"
    os.utime(path, ((NOON + KEPT).timestamp(), (NOON + KEPT).timestamp()))

    assert media.read(TAP, "photo", NOON + KEPT) == b"a picture"
    assert media.read(TAP, "photo", NOON + KEPT, kept_until) is None

    assert not path.exists()
