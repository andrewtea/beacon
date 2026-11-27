"""Radio audio, on its way from a source to Ember.

Ember is sent a WAV file that Beacon made itself, and only that. A call that arrives
as WAV is read and written out again (`plain_wav`), so what is sent is the samples
that were counted and nothing else the file carried. A call that arrives as M4A, as
a recorder that compresses its calls writes it, or as MP3 is decoded by ffmpeg
(`Ffmpeg`). Either way Beacon
knows how long the audio really plays before it pays to have it heard, whatever the
source said its length was.

The audio is radio traffic, which can name people and describe patients, and it
is a file somebody else wrote, so the decoder is given as little as can be:

- **No file of the call is written.** The call is handed to ffmpeg as a file that
  exists only in memory (`memfd_create`, a Linux feature), and what ffmpeg makes of
  it comes back through a pipe.
- **ffmpeg runs confined**, by bubblewrap (`bwrap`): in namespaces of its own, with
  no network, an empty environment, and a file system that is the directories a
  system keeps its programs and their libraries in (`_SYSTEM_DIRS`), read-only, and
  nothing else. `/usr/local`, `/usr/share`, `/opt`, `/etc`, `/home`, and `/var` are
  not there, so neither are Beacon's data, its keys, or the user's files; a path
  that is named as `hidden`, as Beacon's data directory is, is covered over even
  if it lies inside one of those directories. What is left to see is the system's
  own programs and libraries, which are taken to hold no secret. Inside, limits
  keep ffmpeg from writing a file of any size, and bound its memory, its processor
  time, and its open files; outside, a clock and a size limit bound how long it
  runs and how much it answers.

What that does not cover is memory that the system itself writes to a disk, and an
operator has to see to both kinds:

- **Swap.** Run live radio with swap off or encrypted. Nothing here checks it.
- **Core dumps.** Beacon's own process and ffmpeg both run with a core size limit
  of nothing, which stops a core that is written to a file. Linux ignores that
  limit when cores are handed to a collector: piped to a program
  (`kernel.core_pattern` beginning with `|`, as systemd-coredump and apport set
  it) or sent to a socket (beginning with `@`). A collector runs on the host,
  outside any confinement. Beacon's own process is also marked as one the kernel
  does not dump at all (`forbid_core_dumps`), which holds whatever the host does
  with cores. That mark does not survive starting another program, so ffmpeg does
  not have it: `core_dump_problem` reads the host's setting, and
  `beacon.ingest.setup` refuses to hear real radio, and `beacon.taps` to take a
  photo, on a host that has a collector. The setting is read once, when Beacon
  starts: it has to stay as it was for as long as Beacon runs, and nothing here
  watches it.

On a system without `memfd_create` or without a working `bwrap`, nothing is
converted. The mock scenario's calls are WAV and need no ffmpeg anywhere.

A tap's photo comes from the internet as well, and is somebody's own picture, so it
is decoded and drawn again by the same confined ffmpeg (`Ffmpeg.to_jpeg`), under the
same limits. `beacon.taps.media` says what for.
"""

import ctypes
import io
import os
import resource
import shutil
import struct
import subprocess
import sys
import threading
import wave
from collections.abc import Iterable
from pathlib import Path
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
# How long it may take over one photo, by the clock.
PHOTO_SECONDS = 10.0
_UNAVAILABLE = "The converter could not be run on this host just now."
# The address space ffmpeg may have, in kilobytes, and the files it may have open.
_MEMORY_KB = 1_048_576
_OPEN_FILES = 64

# Set inside the confinement, by the shell that then becomes ffmpeg: no core dump,
# no file longer than nothing, and bounds on processor time, memory, and open files.
_LIMITS = (
    f"ulimit -c 0 && ulimit -f 0 && ulimit -t {_TIMEOUT_SECONDS} && "
    f'ulimit -v {_MEMORY_KB} && ulimit -n {_OPEN_FILES} && exec "$@"'
)
# Where a system keeps its programs and their libraries, and nobody keeps anything
# of their own. ffmpeg sees these, read-only, and no other directory: not the rest
# of /usr, where /usr/local and /usr/share are.
_SYSTEM_DIRS = (
    "/usr/bin",
    "/usr/lib",
    "/usr/lib32",
    "/usr/lib64",
    "/usr/libx32",
    "/bin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
)
# The kernel's setting for where a core dump goes.
CORE_PATTERN = Path("/proc/sys/kernel/core_pattern")
# What the dynamic loader reads to find a library.
_LOADER_FILES = ("/etc/ld.so.cache", "/etc/alternatives")


# What Python's `wave` raises on a file that is not what it reads, which for bytes
# from the internet is more than its own error: it also runs off the end of a file,
# seeks past a chunk, and divides by a rate of nothing.
_NOT_WAVE = (wave.Error, EOFError, struct.error, ValueError, RuntimeError, ArithmeticError)


class AudioError(Exception):
    """A clip could not be made into something Ember takes. The text is fixed and
    says nothing of the clip."""


class ConversionUnavailable(Exception):
    """The converter could not be run on this host just now: no memory for the
    file, no process, no thread. It says nothing about the file, which may be a
    good one, and is worth handing over again later. The text is fixed."""


class ConversionStopped(Exception):
    """A conversion was called off because the converter was closed. It says
    nothing about the clip, which can be converted another time."""


class Transcoder(Protocol):
    def to_wav(self, audio: bytes, kind: Convertible) -> bytes:
        """An M4A or MP3 clip as a WAV file, of at most a second more than
        `MAX_SECONDS`. Raises AudioError when it cannot be converted,
        ConversionUnavailable when the transcoder could not be run just now, and
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


def core_dump_problem(pattern: Path = CORE_PATTERN) -> str | None:
    """Why a crash could leave radio audio on a disk on this host, in a sentence,
    or None. A core size limit of nothing, which `forbid_core_dumps` and the
    confinement set, stops a core that is written to a file. Linux ignores the
    limit when cores go to a collector, by a pipe (`|`) or a socket (`@`), so a
    host with one is a problem, and so is one whose setting cannot be read."""
    try:
        setting = pattern.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (
            f"Where this host sends core dumps ({pattern}) cannot be read, so nothing "
            "says that a crash would leave no radio audio on a disk."
        )
    if setting.lstrip().startswith(("|", "@")):
        return (
            "This host hands core dumps to a collector (kernel.core_pattern begins with "
            "| or @), which ignores the limit Beacon sets and could keep radio audio "
            "from a crash. Set kernel.core_pattern to a file name, such as core, and "
            "leave it so while real radio is heard."
        )
    return None


# `prctl` operations, from <linux/prctl.h>.
_PR_GET_DUMPABLE, _PR_SET_DUMPABLE = 3, 4


def forbid_core_dumps() -> bool:
    """Keep a crash of this process from leaving its memory anywhere, and say
    whether that is now so.

    Its core size limit is set to nothing, which stops a core that is written to a
    file. On Linux, where a collector ignores that limit, the process is also marked
    as not dumpable (`PR_SET_DUMPABLE`), and the kernel then makes no core of it
    for a file, a pipe, or a socket. The mark also keeps another process of the
    same user from reading this one's memory or attaching a debugger to it. It does
    not pass to a program this process starts: see `core_dump_problem` for those.

    False when the mark could not be set on Linux. Elsewhere the limit is all there
    is, and all that is needed where nothing is known to ignore it."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, resource.getrlimit(resource.RLIMIT_CORE)[1]))
    if sys.platform != "linux":
        return True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
        return libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def confinement(bwrap: str, hidden: Iterable[Path] = ()) -> list[str]:
    """The start of a command that runs the rest of it confined: no network, no
    environment, the system's program and library directories read-only and no
    other file, the paths in `hidden` covered over where they lie inside those
    directories, and the limits of `_LIMITS`. The module's docstring says why."""
    command = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    shown = []
    for directory in _SYSTEM_DIRS:
        if os.path.islink(directory):
            # /bin is usr/bin on most systems, and is made the same link inside.
            command += ["--symlink", os.readlink(directory), directory]
        elif os.path.isdir(directory):
            command += ["--ro-bind", directory, directory]
            shown.append(directory)
    for file in _LOADER_FILES:
        command += ["--ro-bind-try", file, file]
    for path in hidden:
        private = os.path.realpath(path)
        if not any(_is_inside(private, directory) for directory in shown):
            continue
        if os.path.isdir(private):
            command += ["--tmpfs", private]
        elif os.path.exists(private):
            command += ["--ro-bind", "/dev/null", private]
    # /proc is the confined process's own, and is how it opens the call it is handed.
    command += ["--proc", "/proc", "--dev", "/dev", "--chdir", "/", "--"]
    return [*command, "/bin/sh", "-c", _LIMITS, "sh"]


class Ffmpeg:
    """Converts with the `ffmpeg` program, confined by `bwrap`. Both have to be
    installed, and the system has to be Linux; `problem` says when they are not."""

    def __init__(
        self, executable: str = "ffmpeg", *, bwrap: str = "bwrap", hidden: Iterable[Path] = ()
    ) -> None:
        """`hidden` are paths ffmpeg is not to see even if they lie inside a
        directory it is shown: Beacon's data directory, for one."""
        # Found once, and run by their full paths: the confined shell has no PATH.
        self._ffmpeg = _program(executable)
        self._bwrap = _program(bwrap)
        self._hidden = tuple(Path(path) for path in hidden)
        # Guards `_closed` and `_running`, the conversions that are under way:
        # one for the radio poller, and one for each tap that is being taken.
        self._lock = threading.Lock()
        self._closed = False
        self._running: set[subprocess.Popen[bytes]] = set()

    def problem(self) -> str | None:
        """Why nothing can be converted here, in a sentence, or None when ffmpeg
        ran confined just now."""
        if not hasattr(os, "memfd_create"):
            return "Radio audio is converted in memory, which needs Linux."
        if self._ffmpeg is None:
            return "Radio audio is converted by ffmpeg, which is not installed."
        if not _is_system_file(self._ffmpeg) or any(
            _is_inside(self._ffmpeg, os.path.realpath(path)) for path in self._hidden
        ):
            return (
                "Radio audio is converted by an ffmpeg that is confined to the system's "
                "own program directories, and the one installed is outside them. Install "
                "the system's package."
            )
        if self._bwrap is None:
            return "ffmpeg is run confined by bubblewrap (bwrap), which is not installed."
        try:
            trial = subprocess.run(
                [*confinement(self._bwrap, self._hidden), self._ffmpeg, "-version"],
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
            for process in self._running:
                # bwrap takes everything it confined down with it.
                process.kill()

    def to_wav(self, audio: bytes, kind: Convertible) -> bytes:
        demuxer = _DEMUXERS.get(kind)
        usable = self._ffmpeg is not None and self._bwrap is not None
        # A converter that is not installed converts nothing, today or tomorrow,
        # which `problem` says before one is put to use.
        if demuxer is None or not usable or not hasattr(os, "memfd_create"):
            raise_bare(AudioError("A radio call could not be converted by ffmpeg."))
        pcm = self._convert(
            audio,
            "beacon-radio-call",
            # An MP4 file can say its audio is in another file. It is not followed.
            ["-f", demuxer, *(["-enable_drefs", "0"] if demuxer == "mov" else [])],
            ["-vn", "-t", str(_READ_SECONDS), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le"],
            limit=_READ_SECONDS * SAMPLE_RATE * _SAMPLE_BYTES,
            seconds=_TIMEOUT_SECONDS,
        )
        # Whole samples, and no more of them than were asked for.
        pcm = pcm[: _READ_SECONDS * SAMPLE_RATE * _SAMPLE_BYTES]
        pcm = pcm[: len(pcm) - len(pcm) % _SAMPLE_BYTES]
        if not pcm:
            raise_bare(AudioError("A radio call could not be converted by ffmpeg."))
        return pcm_to_wav(pcm)

    def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
        """A JPEG file decoded and drawn again as a new one: the first picture in
        the file, no wider or taller than `max_side`, with nothing in it that the
        encoder did not write. None when it cannot be decoded, takes longer than
        `PHOTO_SECONDS`, or comes out larger than `max_bytes`.

        The new file has no colour profile, and the picture's colours are not
        converted for the lack of one.

        Raises ConversionUnavailable when ffmpeg could not be run at all, which
        says nothing about the photo, and ConversionStopped when the converter is
        closed, before or meanwhile."""
        fit = f"min(iw,{int(max_side)})", f"min(ih,{int(max_side)})"
        drawn = self._convert(
            photo,
            "beacon-tap-photo",
            # Read as a JPEG and decoded as one, whatever else the bytes could pass for.
            # And strictly: a file whose picture is damaged or cut short is no
            # picture, where ffmpeg would otherwise draw what it could of it.
            ["-err_detect", "explode", "-xerror", "-f", "jpeg_pipe", "-c:v", "mjpeg"],
            [
                "-an",
                "-frames:v",
                "1",
                "-vf",
                f"scale=w='{fit[0]}':h='{fit[1]}':force_original_aspect_ratio=decrease",
                "-pix_fmt",
                "yuvj420p",
                # Nothing is carried over from the file that was read, and the
                # encoder does not sign what it writes.
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
                "-q:v",
                "4",
                "-f",
                "mjpeg",
            ],
            limit=max_bytes,
            seconds=PHOTO_SECONDS,
        )
        return drawn or None

    def _convert(
        self,
        data: bytes,
        name: str,
        read_as: list[str],
        write_as: list[str],
        *,
        limit: int,
        seconds: float,
    ) -> bytes:
        """What the confined ffmpeg makes of `data`, read as `read_as` says and
        written to its output as `write_as` says, or nothing when ffmpeg made
        nothing of it.

        Raises ConversionUnavailable when ffmpeg could not be run at all, and
        ConversionStopped when the converter is closed, before or meanwhile."""
        if self._ffmpeg is None or self._bwrap is None or not hasattr(os, "memfd_create"):
            raise_bare(ConversionUnavailable(_UNAVAILABLE))
        clip = -1
        held = False
        try:
            clip = os.memfd_create(name)
            view = memoryview(data)
            while view:
                view = view[os.write(clip, view) :]
            os.lseek(clip, 0, os.SEEK_SET)
            held = True
        except OSError:
            # No memory for the file, or no more files. Nothing about the file.
            held = False
        if not held:
            if clip >= 0:
                os.close(clip)
            raise_bare(ConversionUnavailable(_UNAVAILABLE))
        try:
            return self._run(
                [
                    *confinement(self._bwrap, self._hidden),
                    self._ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "quiet",
                    "-threads",
                    "1",
                    # The file is read as the one kind of file it is, from this
                    # file and nothing else: a file ffmpeg is left to recognize can
                    # be a playlist, and a playlist can name other files and addresses.
                    "-protocol_whitelist",
                    "file",
                    *read_as,
                    "-i",
                    f"/proc/self/fd/{clip}",
                    *write_as,
                    "pipe:1",
                ],
                clip,
                limit=limit,
                seconds=seconds,
            )
        finally:
            os.close(clip)

    def _run(self, command: list[str], clip: int, *, limit: int, seconds: float) -> bytes:
        """What the command wrote to its output, or nothing when it failed, ran
        for longer than `seconds`, or wrote more than `limit` bytes.

        Raises ConversionUnavailable when the command could not be started or
        watched, and ConversionStopped when the converter is closed, before or
        meanwhile. Either way no process of it is left behind."""
        process: subprocess.Popen[bytes] | None = None
        with self._lock:
            closed = self._closed
            if not closed:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        # What ffmpeg says about a file is not kept.
                        stderr=subprocess.DEVNULL,
                        pass_fds=(clip,),
                        # Nothing of Beacon's environment: a setting such as FFREPORT
                        # would have ffmpeg write a report about the file to a file.
                        env={},
                        cwd="/",
                    )
                except OSError:
                    process = None
                else:
                    self._running.add(process)
        if closed:
            raise_bare(ConversionStopped("The converter is closed."))
        if process is None:
            raise_bare(ConversionUnavailable(_UNAVAILABLE))
        out = bytearray()
        clock: threading.Timer | None = None
        watched = True
        try:
            clock = threading.Timer(seconds, process.kill)
            # A host that is out of threads raises here, with the process running.
            clock.start()
            if process.stdout is not None:
                while len(out) <= limit and (piece := process.stdout.read(65_536)):
                    out += piece
            if len(out) > limit:
                process.kill()
            process.wait()
        except (OSError, RuntimeError):
            watched = False
        finally:
            if clock is not None:
                clock.cancel()
            if process.returncode is None:
                # It was not seen to its end above, whatever went wrong there. It
                # is ended here, and waited for, before anything else is done.
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            with self._lock:
                self._running.discard(process)
                closed = self._closed
        if closed:
            raise_bare(ConversionStopped("The converter is closed."))
        if not watched:
            raise_bare(ConversionUnavailable(_UNAVAILABLE))
        return bytes(out) if process.returncode == 0 and len(out) <= limit else b""


def _program(name: str) -> str | None:
    """The full path of a program, with every link followed, or None."""
    found = shutil.which(name)
    return None if found is None else os.path.realpath(found)


def _is_system_file(path: str) -> bool:
    """Whether a file is in one of the directories the confinement lets be seen."""
    return any(
        _is_inside(path, os.path.realpath(directory))
        for directory in _SYSTEM_DIRS
        if os.path.isdir(directory)
    )


def _is_inside(path: str, directory: str) -> bool:
    """Whether a path is a directory or something in it. Both are real paths."""
    return path == directory or path.startswith(directory.rstrip("/") + "/")
