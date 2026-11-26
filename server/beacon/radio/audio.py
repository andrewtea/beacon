"""Radio audio, on its way from a source to Ember.

Ember takes WAV or MP3. OpenMHz serves a call as M4A, so a call is converted to WAV
first, by ffmpeg. The audio is radio traffic, which can name people and describe
patients, so it is never written to a disk: it is handed to ffmpeg as a file that
exists only in memory, and what ffmpeg makes of it comes back through a pipe.

That in-memory file is a Linux feature (`memfd_create`). On another system an M4A
call cannot be converted and is refused; the mock scenario's calls are WAV already
and need no conversion anywhere.
"""

import io
import os
import subprocess
import wave
from typing import Literal, Protocol

from beacon.errors import raise_bare

# What Ember's `input_audio` takes.
EmberFormat = Literal["wav", "mp3"]
Container = Literal["wav", "mp3", "m4a"]

# What a call is converted to: speech needs no more, and a small clip is a small request.
SAMPLE_RATE = 16_000
_SAMPLE_BYTES = 2

# No call is this long. What is past it is not converted.
MAX_SECONDS = 120
# How long ffmpeg may take over one call.
_TIMEOUT_SECONDS = 30.0


class AudioError(Exception):
    """A clip could not be made into something Ember takes. The text is fixed and
    says nothing of the clip."""


class Transcoder(Protocol):
    def to_wav(self, m4a: bytes) -> bytes:
        """An M4A clip as a WAV file. Raises AudioError when it cannot be converted."""
        ...


def container(audio: bytes) -> Container | None:
    """Which kind of audio file this is, by how it begins, or None for anything else.
    It is what the bytes are that counts, not what the address they came from ends in."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "wav"
    if audio[4:8] == b"ftyp":
        return "m4a"
    # An MP3 begins with an ID3 tag, or with a frame, whose first eleven bits are set.
    if audio[:3] == b"ID3" or (len(audio) > 1 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def wav_seconds(audio: bytes) -> float | None:
    """How long a WAV file plays, or None when it cannot be read as one."""
    try:
        with wave.open(io.BytesIO(audio)) as clip:
            return clip.getnframes() / clip.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def pcm_to_wav(pcm: bytes) -> bytes:
    """16-bit mono samples at `SAMPLE_RATE` as a WAV file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(_SAMPLE_BYTES)
        clip.setframerate(SAMPLE_RATE)
        clip.writeframes(pcm)
    return out.getvalue()


class Ffmpeg:
    """Converts with the `ffmpeg` program, which has to be installed."""

    def __init__(self, executable: str = "ffmpeg") -> None:
        self._executable = executable

    def to_wav(self, m4a: bytes) -> bytes:
        if not hasattr(os, "memfd_create"):
            raise_bare(AudioError("Radio audio is converted in memory, which needs Linux."))
        clip = os.memfd_create("beacon-radio-call")
        try:
            os.write(clip, m4a)
            os.lseek(clip, 0, os.SEEK_SET)
            command = [
                self._executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "quiet",
                # The clip is read as MP4 and nothing else, from this file and
                # nothing else: a file ffmpeg is left to recognize can be a
                # playlist, and a playlist can name other files and addresses.
                "-protocol_whitelist",
                "file",
                # An MP4 file can say its audio is in another file. It is not followed.
                "-enable_drefs",
                "0",
                "-f",
                "mov",
                "-i",
                f"/proc/self/fd/{clip}",
                "-vn",
                "-t",
                str(MAX_SECONDS),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "s16le",
                "pipe:1",
            ]
            try:
                done = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    # What ffmpeg says about a clip is not kept.
                    stderr=subprocess.DEVNULL,
                    pass_fds=(clip,),
                    timeout=_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                done = None
        finally:
            os.close(clip)
        pcm = b"" if done is None or done.returncode != 0 else done.stdout
        # Whole samples, and no more of them than the longest call has.
        pcm = pcm[: MAX_SECONDS * SAMPLE_RATE * _SAMPLE_BYTES]
        pcm = pcm[: len(pcm) - len(pcm) % _SAMPLE_BYTES]
        if not pcm:
            # Raised outside the handler: a timeout's error holds what ffmpeg wrote.
            raise_bare(AudioError("A radio call could not be converted by ffmpeg."))
        return pcm_to_wav(pcm)
