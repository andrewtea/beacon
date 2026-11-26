"""Radio audio, on its way from a source to Ember.

Ember is sent a WAV file that Beacon made itself, and only that. A call that arrives
as WAV is read and written out again (`plain_wav`), so what is sent is the samples
that were counted and nothing else the file carried. A call that arrives as M4A, as
OpenMHz serves it, or as MP3 is decoded by ffmpeg (`Ffmpeg`). Either way Beacon
knows how long the audio really plays before it pays to have it heard, whatever the
source said its length was.

The audio is radio traffic, which can name people and describe patients, and it
comes from the internet, so the decoder is given as little as can be:

- **No file of the call is written.** The call is handed to ffmpeg as a file that
  exists only in memory (`memfd_create`, a Linux feature), and what ffmpeg makes of
  it comes back through a pipe.
- **ffmpeg runs confined**, by bubblewrap (`bwrap`): in namespaces of its own, with
  no network, an empty environment, and a file system that is the system's programs
  and libraries, read-only, and nothing else. Beacon's data, its keys, and the
  user's files are not there to be read. Inside, limits keep it from writing a file
  of any size or a core dump, and bound its memory, its processor time, and its
  open files; outside, a clock and a size limit bound how long it runs and how much
  it answers.

What that does not cover, and an operator has to: memory can be written to disk by
the system itself, as swap, or as a core dump of Beacon's own process, which holds
the call while it is worked on. Run live radio with swap off or encrypted.
`beacon.ingest.setup` turns Beacon's own core dumps off when it builds a poller for
real radio.

On a system without `memfd_create` or without a working `bwrap`, nothing is
converted. The mock scenario's calls are WAV and need no ffmpeg anywhere.
"""

import io
import os
import shutil
import struct
import subprocess
import threading
import wave
from typing import Literal, Protocol

from beacon.errors import raise_bare

# What Ember's `input_audio` takes.
EmberFormat = Literal["wav", "mp3"]
Container = Literal["wav", "mp3", "m4a"]
# What is decoded by ffmpeg, and the one demuxer each is read with.
Convertible = Literal["m4a", "mp3"]
_DEMUXERS: dict[str, str] = {"m4a": "mov", "mp3": "mp3"}

# What a call is converted to: speech needs no more, and a small clip is a small request.
SAMPLE_RATE = 16_000
_SAMPLE_BYTES = 2

# No call worth hearing is longer than this.
MAX_SECONDS = 120
# A clip is read a second past that and no further, so that one that is longer is
# seen to be longer, and can be refused instead of cut short and sent.
_READ_SECONDS = MAX_SECONDS + 1
# How long ffmpeg may take over one call, by the clock and of processor time.
_TIMEOUT_SECONDS = 30
# The address space ffmpeg may have, in kilobytes, and the files it may have open.
_MEMORY_KB = 1_048_576
_OPEN_FILES = 64

# Set inside the confinement, by the shell that then becomes ffmpeg: no core dump,
# no file longer than nothing, and bounds on processor time, memory, and open files.
_LIMITS = (
    f"ulimit -c 0 && ulimit -f 0 && ulimit -t {_TIMEOUT_SECONDS} && "
    f'ulimit -v {_MEMORY_KB} && ulimit -n {_OPEN_FILES} && exec "$@"'
)
# Where a system keeps its programs and libraries. ffmpeg sees these, read-only.
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# What the dynamic loader reads to find a library.
_LOADER_FILES = ("/etc/ld.so.cache", "/etc/alternatives")


# What Python's `wave` raises on a file that is not what it reads, which for bytes
# from the internet is more than its own error: it also runs off the end of a file,
# seeks past a chunk, and divides by a rate of nothing.
_NOT_WAVE = (wave.Error, EOFError, struct.error, ValueError, RuntimeError, ArithmeticError)


class AudioError(Exception):
    """A clip could not be made into something Ember takes. The text is fixed and
    says nothing of the clip."""


class ConversionStopped(Exception):
    """A conversion was called off because the converter was closed. It says
    nothing about the clip, which can be converted another time."""


class Transcoder(Protocol):
    def to_wav(self, audio: bytes, kind: Convertible) -> bytes:
        """An M4A or MP3 clip as a WAV file, of at most a second more than
        `MAX_SECONDS`. Raises AudioError when it cannot be converted, and
        ConversionStopped when the transcoder was closed."""
        ...

    def close(self) -> None:
        """Call off a conversion that is running, and make no more."""
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
    """How long a WAV file says it plays, or None when it cannot be read as one.
    For a file `plain_wav` or a transcoder made, that is how long it does play."""
    try:
        with wave.open(io.BytesIO(audio)) as clip:
            return clip.getnframes() / clip.getframerate()
    except _NOT_WAVE:
        return None


def plain_wav(audio: bytes) -> bytes:
    """A WAV file of uncompressed samples, written out again as a file that holds
    its samples and nothing more: the samples that are really there, whatever its
    header said of their number, and at most a second more than `MAX_SECONDS` of
    them.

    Raises AudioError when it is not such a file, or holds no samples."""
    frames = b""
    try:
        with wave.open(io.BytesIO(audio)) as clip:
            channels, width, rate = clip.getnchannels(), clip.getsampwidth(), clip.getframerate()
            if 1 <= channels <= 2 and 1 <= width <= 4 and 4_000 <= rate <= 96_000:
                frames = clip.readframes(rate * _READ_SECONDS)
                frames = frames[: len(frames) - len(frames) % (channels * width)]
    except _NOT_WAVE:
        frames = b""
    if not frames:
        # Raised outside the handler, which holds the file.
        raise_bare(AudioError("A radio call is not a WAV file of plain samples."))
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(width)
        clip.setframerate(rate)
        clip.writeframes(frames)
    return out.getvalue()


def pcm_to_wav(pcm: bytes) -> bytes:
    """16-bit mono samples at `SAMPLE_RATE` as a WAV file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(_SAMPLE_BYTES)
        clip.setframerate(SAMPLE_RATE)
        clip.writeframes(pcm)
    return out.getvalue()


def confinement(bwrap: str) -> list[str]:
    """The start of a command that runs the rest of it confined: no network, no
    environment, the system's programs and libraries read-only and no other file,
    and the limits of `_LIMITS`. The module's docstring says why."""
    command = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    for directory in _SYSTEM_DIRS:
        if os.path.islink(directory):
            # /bin is usr/bin on most systems, and is made the same link inside.
            command += ["--symlink", os.readlink(directory), directory]
        elif os.path.isdir(directory):
            command += ["--ro-bind", directory, directory]
    for file in _LOADER_FILES:
        command += ["--ro-bind-try", file, file]
    # /proc is the confined process's own, and is how it opens the call it is handed.
    command += ["--proc", "/proc", "--dev", "/dev", "--chdir", "/", "--"]
    return [*command, "/bin/sh", "-c", _LIMITS, "sh"]


class Ffmpeg:
    """Converts with the `ffmpeg` program, confined by `bwrap`. Both have to be
    installed, and the system has to be Linux; `problem` says when they are not."""

    def __init__(self, executable: str = "ffmpeg", *, bwrap: str = "bwrap") -> None:
        # Found once, and run by their full paths: the confined shell has no PATH.
        self._ffmpeg = _program(executable)
        self._bwrap = _program(bwrap)
        # Guards `_closed` and `_running`, the conversion that is under way.
        self._lock = threading.Lock()
        self._closed = False
        self._running: subprocess.Popen[bytes] | None = None

    def problem(self) -> str | None:
        """Why nothing can be converted here, in a sentence, or None when ffmpeg
        ran confined just now."""
        if not hasattr(os, "memfd_create"):
            return "Radio audio is converted in memory, which needs Linux."
        if self._ffmpeg is None:
            return "Radio audio is converted by ffmpeg, which is not installed."
        if not _is_system_file(self._ffmpeg):
            return (
                "Radio audio is converted by an ffmpeg that is confined to the system's "
                "own directories, and the one installed is outside them. Install the "
                "system's package."
            )
        if self._bwrap is None:
            return "ffmpeg is run confined by bubblewrap (bwrap), which is not installed."
        try:
            trial = subprocess.run(
                [*confinement(self._bwrap), self._ffmpeg, "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={},
                cwd="/",
                timeout=_TIMEOUT_SECONDS,
                check=False,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            trial = None
        if trial != 0:
            return (
                "ffmpeg could not be run confined by bubblewrap (bwrap), which needs "
                "user namespaces that an unprivileged user may create."
            )
        return None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._running is not None:
                # bwrap takes everything it confined down with it.
                self._running.kill()

    def to_wav(self, audio: bytes, kind: Convertible) -> bytes:
        demuxer = _DEMUXERS.get(kind)
        usable = self._ffmpeg is not None and self._bwrap is not None
        if demuxer is None or not usable or not hasattr(os, "memfd_create"):
            raise_bare(AudioError("A radio call could not be converted by ffmpeg."))
        clip = os.memfd_create("beacon-radio-call")
        try:
            view = memoryview(audio)
            while view:
                view = view[os.write(clip, view) :]
            os.lseek(clip, 0, os.SEEK_SET)
            pcm = self._run(
                [
                    *confinement(self._bwrap),
                    self._ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "quiet",
                    "-threads",
                    "1",
                    # The clip is read as the one kind of file it is, from this
                    # file and nothing else: a file ffmpeg is left to recognize can
                    # be a playlist, and a playlist can name other files and addresses.
                    "-protocol_whitelist",
                    "file",
                    "-f",
                    demuxer,
                    # An MP4 file can say its audio is in another file. It is not followed.
                    *(["-enable_drefs", "0"] if demuxer == "mov" else []),
                    "-i",
                    f"/proc/self/fd/{clip}",
                    "-vn",
                    "-t",
                    str(_READ_SECONDS),
                    "-ac",
                    "1",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                clip,
            )
        finally:
            os.close(clip)
        # Whole samples, and no more of them than were asked for.
        pcm = pcm[: _READ_SECONDS * SAMPLE_RATE * _SAMPLE_BYTES]
        pcm = pcm[: len(pcm) - len(pcm) % _SAMPLE_BYTES]
        if not pcm:
            raise_bare(AudioError("A radio call could not be converted by ffmpeg."))
        return pcm_to_wav(pcm)

    def _run(self, command: list[str], clip: int) -> bytes:
        """What the command wrote to its output, or nothing when it failed, ran
        over its time, or wrote more than a clip can be.

        Raises ConversionStopped when the converter is closed, before or meanwhile."""
        limit = _READ_SECONDS * SAMPLE_RATE * _SAMPLE_BYTES
        with self._lock:
            if self._closed:
                raise_bare(ConversionStopped("The converter is closed."))
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    # What ffmpeg says about a clip is not kept.
                    stderr=subprocess.DEVNULL,
                    pass_fds=(clip,),
                    # Nothing of Beacon's environment: a setting such as FFREPORT
                    # would have ffmpeg write a report about the clip to a file.
                    env={},
                    cwd="/",
                )
            except OSError:
                process = None
            self._running = process
        if process is None or process.stdout is None:
            return b""
        out = bytearray()
        clock = threading.Timer(_TIMEOUT_SECONDS, process.kill)
        clock.start()
        try:
            while len(out) <= limit and (piece := process.stdout.read(65_536)):
                out += piece
            if len(out) > limit:
                process.kill()
            process.stdout.close()
            process.wait()
        finally:
            clock.cancel()
            with self._lock:
                self._running = None
                closed = self._closed
        if closed:
            raise_bare(ConversionStopped("The converter is closed."))
        return bytes(out) if process.returncode == 0 and len(out) <= limit else b""


def _program(name: str) -> str | None:
    """The full path of a program, with every link followed, or None."""
    found = shutil.which(name)
    return None if found is None else os.path.realpath(found)


def _is_system_file(path: str) -> bool:
    """Whether a file is in one of the directories the confinement lets be seen."""
    return any(
        path.startswith(os.path.realpath(directory) + "/")
        for directory in _SYSTEM_DIRS
        if os.path.isdir(directory)
    )
