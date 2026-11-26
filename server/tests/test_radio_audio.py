"""Radio audio on its way to Ember: what kind of file it is, and M4A made into WAV."""

import io
import math
import os
import shutil
import subprocess
import sys
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

from beacon.radio import AudioError, Ffmpeg
from beacon.radio.audio import SAMPLE_RATE, container, pcm_to_wav, wav_seconds

_CAN_CONVERT = sys.platform == "linux" and shutil.which("ffmpeg") is not None
if os.environ.get("CI") and not _CAN_CONVERT:
    # CI installs ffmpeg, so that the conversion is not quietly left untested there.
    raise RuntimeError("CI runs the audio conversion tests, which need ffmpeg on Linux.")
needs_ffmpeg = pytest.mark.skipif(
    not _CAN_CONVERT, reason="Converting radio audio needs ffmpeg, on Linux."
)


def tone(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    """A generated tone as a WAV file. Nothing in these tests is a recording."""
    samples = b"".join(
        round(8000 * math.sin(2 * math.pi * 440 * n / rate)).to_bytes(2, "little", signed=True)
        for n in range(round(seconds * rate))
    )
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(samples)
    return out.getvalue()


def as_m4a(wav: bytes, directory: Path) -> bytes:
    """The clip as ffmpeg writes an M4A file, which is how OpenMHz serves a call."""
    target = directory / "call.m4a"
    subprocess.run(
        ["ffmpeg", "-loglevel", "quiet", "-y", "-f", "wav", "-i", "pipe:0", "-c:a", "aac", target],
        input=wav,
        check=True,
    )
    return target.read_bytes()


def test_a_file_is_known_by_how_it_begins_and_not_by_its_name() -> None:
    assert container(tone(0.1)) == "wav"
    assert container(b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00") == "m4a"
    assert container(b"ID3\x04\x00\x00\x00\x00\x00\x00") == "mp3"
    assert container(b"\xff\xfb\x90\x00") == "mp3"
    for other in (b"", b"RIFF", b"<html>call.m4a</html>", b"#EXTM3U\n", b"OggS\x00"):
        assert container(other) is None


def test_a_wav_file_says_how_long_it_plays() -> None:
    assert wav_seconds(tone(2.5)) == pytest.approx(2.5)
    assert wav_seconds(pcm_to_wav(b"\x00\x00" * SAMPLE_RATE)) == pytest.approx(1.0)
    assert wav_seconds(b"not a wav file") is None
    assert wav_seconds(b"") is None


@needs_ffmpeg
def test_an_m4a_call_becomes_a_mono_wav_of_the_same_length(tmp_path: Path) -> None:
    m4a = as_m4a(tone(3.0, rate=8_000), tmp_path)
    assert container(m4a) == "m4a"

    wav = Ffmpeg().to_wav(m4a)

    assert container(wav) == "wav"
    with wave.open(io.BytesIO(wav)) as clip:
        assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (1, 2, 16_000)
    # AAC pads a clip by a few hundredths of a second.
    assert wav_seconds(wav) == pytest.approx(3.0, abs=0.15)


@needs_ffmpeg
def test_a_call_is_converted_without_a_file_of_it_being_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m4a = as_m4a(tone(1.0), tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    # Wherever a program would put a file it was not told where to put.
    monkeypatch.chdir(empty)
    for variable in ("TMPDIR", "TEMP", "TMP", "HOME"):
        monkeypatch.setenv(variable, str(empty))

    assert wav_seconds(Ffmpeg().to_wav(m4a)) == pytest.approx(1.0, abs=0.15)

    assert list(empty.iterdir()) == []


def _while_handling(error: Exception, call: Callable[[], object]) -> None:
    try:
        raise error
    except type(error):
        call()


@needs_ffmpeg
@pytest.mark.parametrize(
    "not_a_call",
    [
        b"",
        b"\x00\x00\x00\x1cftypM4A and then nothing that is audio",
        b"Jane Doe, 1458 Hayes Street",
        # A playlist, which ffmpeg left to itself would follow to other files.
        b"#EXTM3U\n#EXTINF:1,\nfile:///etc/hostname\n",
        b"ffconcat version 1.0\nfile /etc/hostname\n",
    ],
)
def test_what_is_not_an_m4a_clip_is_refused_with_a_fixed_sentence(not_a_call: bytes) -> None:
    with pytest.raises(AudioError) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: Ffmpeg().to_wav(not_a_call))

    assert str(failure.value) == "A radio call could not be converted by ffmpeg."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)


def test_a_missing_ffmpeg_is_the_same_fixed_sentence() -> None:
    if not hasattr(os, "memfd_create"):
        pytest.skip("Converting radio audio in memory needs Linux.")

    with pytest.raises(AudioError, match=r"^A radio call could not be converted by ffmpeg\.$"):
        Ffmpeg("/nonexistent/ffmpeg").to_wav(b"\x00\x00\x00\x1cftypM4A ")
