"""The invented story's radio calls, left as files for Muse Ember to hear.

When a stand-in works on the story, its calls are tones on a shelf of the story's
own (`beacon.scenario.radio.StoryShelf`), and the stand-in fills in what was "said".
A tone says nothing, so a paid Ember is never sent one. When Muse Ember works on the
story (`beacon.telling`), each call is **synthesized speech of invented words**
(`RadioCall.said`, by ffmpeg's `flite` voice), left in a directory in the layout
trunk-recorder writes, each when its moment in the story has come, as a recorder
leaves a call when it has been made. The real reader of recorded calls
(`beacon.ingest.recordings.Directory`) then reads them like any other.

Nothing here was said on a radio, and nothing here is a recording of anybody. The
directory is marked `BEACON-GENERATED` before anything else is written into it, so
its rows are `mock` ones whoever hears them and the live store refuses them. A call
in which nothing is said (wind across a microphone) is noise made here, which Ember
is to find unintelligible; a click is too short to be heard at all.

The directory holds one telling's calls. A story that begins anew has the calls of
the one before deleted first: they were about another story, at other times.
"""

import json
import os
import random
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from beacon.ingest.recordings import GENERATED_MARK
from beacon.radio.audio import SAMPLE_RATE, pcm_to_wav, wav_seconds
from beacon.radio.speech import synthesize
from beacon.scenario.fire import INVENTED
from beacon.scenario.radio import _FREQUENCY, CALLS, SYSTEM, RadioCall

# Where the calls are left, under the data directory.
CALLS_DIRECTORY = ("scenario", "calls")
# The file that says which telling the calls are of: when its story began.
_TELLING_FILE = "telling.json"
_MARK_TEXT = (
    "Generated audio. No radio.\n"
    "Made by Beacon's mock scenario (beacon.scenario.recorder): the invented story's "
    "radio calls, as synthesized speech of invented words.\n"
)

# How words become a WAV file: `beacon.radio.speech.synthesize`, unless a test
# hands in something that needs no ffmpeg.
Voice = Callable[[Path, str], bytes]


class RecorderError(Exception):
    """The story's calls could not be left as files. The text is fixed."""


class StoryRecorder:
    """Leaves the calls of a story that began at `start` in `root`, each when its
    moment has come (`leave`)."""

    def __init__(self, root: Path, start: datetime, voice: Voice | None = None) -> None:
        if start.utcoffset() is None:
            raise ValueError("The story's start must carry a time zone.")
        self.root = root
        self._start = start
        self._voice = synthesize if voice is None else voice
        # The calls this process has left or found left, by their place in `CALLS`.
        self._left: set[int] = set()

    def begin(self) -> None:
        """Make the directory, marked as generated audio, and empty it of the calls
        of any other telling.

        Raises RecorderError when the directory is somebody else's (it holds files
        and has no mark, or is a link), or could not be written."""
        root, mark = self.root, self.root / GENERATED_MARK
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise RecorderError("The story's calls are left in a directory, not a link to one.")
        marked = mark.is_file() and not mark.is_symlink()
        if root.is_dir() and any(root.iterdir()) and not marked:
            raise RecorderError(
                f"The directory for the story's calls holds files and is not marked "
                f"{GENERATED_MARK}: generated calls are not left among anything else."
            )
        try:
            root.mkdir(parents=True, exist_ok=True)
            # The mark first: nothing generated is ever in a directory without it.
            mark.write_text(_MARK_TEXT, encoding="utf-8")
            if self._told() != self._start:
                # Another telling's calls, or none: begun with nothing there.
                (root / _TELLING_FILE).unlink(missing_ok=True)
                shutil.rmtree(root / SYSTEM, ignore_errors=True)
                if (root / SYSTEM).exists():
                    raise OSError("The calls of an earlier telling could not be deleted.")
                beside = root / (_TELLING_FILE + ".part")
                beside.write_text(json.dumps({"start": self._start.isoformat()}), encoding="utf-8")
                os.replace(beside, root / _TELLING_FILE)
        except OSError as error:
            raise RecorderError("The story's calls could not be left as files.") from error

    def leave(self, upto: datetime) -> int:
        """Leave every call whose moment is `upto` or before and that is not there
        yet, and say how many were left now.

        Raises RecorderError when a call could not be synthesized or written."""
        left = 0
        try:
            for index, call in enumerate(CALLS):
                if index in self._left or self.time_of(index) > upto:
                    continue
                left += self._write(index, call)
                self._left.add(index)
        except OSError as error:
            raise RecorderError("A call of the story could not be left as a file.") from error
        return left

    def time_of(self, index: int) -> datetime:
        return self._start + timedelta(minutes=CALLS[index].minute)

    def _told(self) -> datetime | None:
        """When the story began whose calls the directory holds, or None."""
        try:
            said = json.loads((self.root / _TELLING_FILE).read_text(encoding="utf-8"))
            return datetime.fromisoformat(said["start"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, index: int, call: RadioCall) -> int:
        at = self.time_of(index).astimezone(UTC)
        day = self.root.joinpath(SYSTEM, str(at.year), str(at.month), str(at.day))
        stem = (
            f"{call.talkgroup}-{int(at.timestamp())}.{at.microsecond // 1000:03d}"
            f"_{_FREQUENCY}-call_{index}"
        )
        if (day / f"{stem}.json").is_file():
            # Left by this telling before the server was started again.
            return 0
        day.mkdir(parents=True, exist_ok=True)
        audio = (
            _noise(index, call.seconds) if call.said is None else self._voice(self.root, call.said)
        )
        seconds = wav_seconds(audio)
        if seconds is None:
            raise OSError("A call of the story is no WAV file.")
        # The audio before the JSON, and each whole: a reader that finds a call's
        # JSON finds all of its audio.
        _write_whole(day / f"{stem}.wav", audio)
        _write_whole(day / f"{stem}.json", json.dumps(_details(at, seconds, call)).encode())
        return 1


def _write_whole(path: Path, body: bytes) -> None:
    beside = path.with_name(path.name + ".part")
    beside.write_bytes(body)
    os.replace(beside, path)


def _details(at: datetime, seconds: float, call: RadioCall) -> dict[str, Any]:
    """The JSON trunk-recorder would write for the call, as far as Beacon reads it."""
    start_ms = int(at.timestamp()) * 1000 + at.microsecond // 1000
    length_ms = round(seconds * 1000)
    return {
        "freq": _FREQUENCY,
        "start_time": start_ms // 1000,
        "stop_time": (start_ms + length_ms) // 1000 + 1,
        "start_time_ms": start_ms,
        "stop_time_ms": start_ms + length_ms,
        "emergency": 0,
        "encrypted": 0,
        "call_length": round(seconds),
        "call_length_ms": length_ms,
        "talkgroup": call.talkgroup,
        "talkgroup_tag": INVENTED,
        "audio_type": "digital",
        "short_name": SYSTEM,
        "freqList": [],
        "srcList": [],
    }


def _noise(index: int, seconds: float) -> bytes:
    """Noise as long as the call, the same for a call every time: wind across a
    microphone, in which nothing is said."""
    drawn = random.Random(index)
    samples = bytearray()
    for _ in range(round(seconds * SAMPLE_RATE)):
        samples += drawn.randint(-3000, 3000).to_bytes(2, "little", signed=True)
    return pcm_to_wav(bytes(samples))
