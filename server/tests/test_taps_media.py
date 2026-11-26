"""A tap's photo and clip: what is stripped from them, and how long they are kept."""

import io
import shutil
import stat
import subprocess
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beacon.taps import media as tap_media
from beacon.taps.media import (
    KEPT,
    MAX_CLIP_BYTES,
    MAX_CLIP_SECONDS,
    MAX_PHOTO_BYTES,
    MediaError,
    MediaFullError,
    TapMedia,
    clean_clip,
    clean_photo,
)

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
TAP = "tap-AAAAAAAAAAAA"
OTHER = "tap-BBBBBBBBBBBB"

# What a phone's file says besides its picture. All of it is invented.
SECRETS = (b"GPSLatitude 37.7763", b"Jane Doe's iPhone", b"taken at home", b"a second picture")


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
    cleaned = clean_photo(jpeg())

    # What is left is the file a camera with nothing to say would have written.
    assert cleaned.content == jpeg(extras=False)
    assert (cleaned.width, cleaned.height) == (640, 480)
    for secret in SECRETS:
        assert secret not in cleaned.content


def test_cleaning_a_photo_that_is_already_clean_changes_nothing() -> None:
    assert clean_photo(jpeg(extras=False)).content == jpeg(extras=False)


@pytest.mark.parametrize("marker", [0xE3, 0xED, 0xEF, 0xF0, 0xF7, 0xC8, 0xDE, 0x02, 0xBF])
def test_a_segment_no_decoder_needs_is_dropped_whatever_it_is(marker: int) -> None:
    # Application segments, and markers reserved for uses nobody has given them.
    hidden = jpeg(extras=False).replace(QUANTIZATION, segment(marker, SECRETS[1]) + QUANTIZATION)

    assert clean_photo(hidden).content == jpeg(extras=False)


def test_a_progressive_picture_keeps_every_scan_and_loses_what_is_between_them() -> None:
    scans = SCAN + PICTURE + segment(0xE1, SECRETS[0]) + HUFFMAN + SCAN + PICTURE
    photo = jpeg(extras=False).replace(SCAN + PICTURE, scans)

    assert clean_photo(photo).content == jpeg(extras=False).replace(
        SCAN + PICTURE, SCAN + PICTURE + HUFFMAN + SCAN + PICTURE
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make and show a JPEG")
def test_a_real_picture_shows_the_same_after_it_is_cleaned(tmp_path: Path) -> None:
    made = tmp_path / "made.jpg"
    draw = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48", "-frames:v", "1"]
    subprocess.run([*draw, str(made)], check=True)
    real = made.read_bytes()
    photo = real[:2] + segment(0xE1, b"Exif\x00\x00" + SECRETS[0]) + real[2:] + SECRETS[3]

    cleaned = clean_photo(photo)

    def pixels(file: bytes, name: str) -> bytes:
        path = tmp_path / name
        path.write_bytes(file)
        show = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
        return subprocess.run(show, check=True, capture_output=True).stdout

    assert (cleaned.width, cleaned.height) == (64, 48)
    assert SECRETS[0] not in cleaned.content
    assert SECRETS[3] not in cleaned.content
    assert pixels(cleaned.content, "cleaned.jpg") == pixels(real, "real.jpg") != b""


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
        clean_photo(photo)

    assert str(refused.value) == "A tap's photo is a JPEG file of at most four megabytes."
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


def test_a_photo_larger_than_a_photo_may_be_is_refused() -> None:
    padded = jpeg(extras=False) + bytes(MAX_PHOTO_BYTES)

    with pytest.raises(MediaError, match="at most four megabytes"):
        clean_photo(padded)


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
    media = TapMedia(tmp_path / "taps")

    kept_until = media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(TAP, "clip", b"a sound", NOON)

    assert kept_until == NOON + KEPT
    assert media.read(TAP, "photo", NOON + timedelta(minutes=59)) == b"a picture"
    assert media.read(TAP, "clip", NOON + timedelta(minutes=59)) == b"a sound"
    assert media.read(OTHER, "photo", NOON) is None
    files = sorted((tmp_path / "taps").iterdir())
    assert [file.name for file in files] == [f"{TAP}.clip.wav", f"{TAP}.photo.jpg"]
    assert {stat.S_IMODE(file.stat().st_mode) for file in files} == {0o600}
    assert stat.S_IMODE((tmp_path / "taps").stat().st_mode) == 0o700


def test_media_whose_hour_is_over_is_not_handed_out_and_is_deleted(tmp_path: Path) -> None:
    media = TapMedia(tmp_path)
    media.keep(TAP, "photo", b"a picture", NOON)
    media.keep(OTHER, "clip", b"a sound", NOON + timedelta(minutes=30))

    # Asked for at the end of its hour, before any sweep has got to it.
    assert media.read(TAP, "photo", NOON + KEPT) is None
    assert [file.name for file in tmp_path.iterdir()] == [f"{OTHER}.clip.wav"]

    assert media.sweep(NOON + KEPT) == 0
    assert media.sweep(NOON + KEPT + timedelta(minutes=30)) == 1
    assert list(tmp_path.iterdir()) == []


def test_a_sweep_deletes_what_a_stopped_server_left_half_written(tmp_path: Path) -> None:
    media = TapMedia(tmp_path)
    media.keep(TAP, "photo", b"a picture", NOON)
    (tmp_path / f".{OTHER}.photo.jpg.part").write_bytes(b"half a pic")

    assert media.sweep(NOON) == 1

    assert [file.name for file in tmp_path.iterdir()] == [f"{TAP}.photo.jpg"]


def test_dropping_a_tap_deletes_its_media_and_no_other_taps(tmp_path: Path) -> None:
    media = TapMedia(tmp_path)
    for tap in (TAP, OTHER):
        media.keep(tap, "photo", b"a picture", NOON)
        media.keep(tap, "clip", b"a sound", NOON)

    media.drop(TAP)

    assert sorted(file.name for file in tmp_path.iterdir()) == [
        f"{OTHER}.clip.wav",
        f"{OTHER}.photo.jpg",
    ]


def test_all_the_media_together_stays_under_a_limit_and_expired_media_makes_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tap_media, "MAX_STORED_BYTES", 25)
    media = TapMedia(tmp_path)
    media.keep(TAP, "photo", b"0123456789", NOON)
    media.keep(TAP, "clip", b"0123456789", NOON)

    with pytest.raises(MediaFullError) as full:
        media.keep(OTHER, "photo", b"0123456789", NOON + timedelta(minutes=59))

    assert str(full.value) == "No more photos or clips can be kept just now."
    assert media.read(OTHER, "photo", NOON + timedelta(minutes=59)) is None
    # An hour after the first two, they are deleted to make room.
    media.keep(OTHER, "photo", b"0123456789", NOON + KEPT)
    assert [file.name for file in tmp_path.iterdir()] == [f"{OTHER}.photo.jpg"]


@pytest.mark.parametrize(
    "tap_id", ["", "short", "../../etc/passwd", "tap/AAAAAAAA", "a" * 41, None]
)
def test_a_name_that_is_not_shaped_as_a_taps_id_is_never_made_into_a_path(
    tmp_path: Path, tap_id: object
) -> None:
    media = TapMedia(tmp_path / "taps")

    with pytest.raises(ValueError, match="not a tap's id"):
        media.keep(tap_id, "photo", b"a picture", NOON)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a tap's id"):
        media.read(TAP, "video", NOON)  # type: ignore[arg-type]

    assert not (tmp_path / "taps").exists()
