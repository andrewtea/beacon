"""Synthesized speech: words said by ffmpeg's `flite` voice, as a WAV file.

It is what generated calls are made of when a paid Ember is to hear them
(`beacon.scale.generate`, and the invented story's calls in
`beacon.scenario.radio.SpokenShelf`): clean synthetic speech, which is no radio. The
words are Beacon's own, a script made from public records or the invented story's
lines, so the two files ffmpeg works on here are no recording of anybody.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from beacon.radio.audio import SAMPLE_RATE, plain_wav

_TIMEOUT_SECONDS = 30
# The names of the two files, in a directory of their own for each synthesis.
_WORDS = "words.txt"
_SPEECH = "speech.wav"


def can_speak() -> bool:
    """Whether this host's ffmpeg has the `flite` voice."""
    try:
        listed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return listed.returncode == 0 and b" flite " in listed.stdout


def synthesize(words: str) -> bytes:
    """The words as synthesized speech, a WAV file at Beacon's sample rate.

    The two files ffmpeg works on, the words and the speech, are in a directory
    made for this one synthesis, with a name nobody can guess and open to this
    user alone (`tempfile.mkdtemp`), and it is deleted with them when this returns.
    Nothing is written under a name somebody else could have put there first, and
    nothing that was there before is deleted.

    Raises OSError when ffmpeg could not synthesize them."""
    try:
        with tempfile.TemporaryDirectory(prefix="beacon-speech-") as made:
            work = Path(made)
            (work / _WORDS).write_text(words, encoding="utf-8")
            done = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-n",
                    "-f",
                    "lavfi",
                    "-i",
                    f"flite=textfile={_WORDS}:voice=slt",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    _SPEECH,
                ],
                cwd=work,
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
            if done.returncode != 0:
                raise OSError("ffmpeg could not synthesize a call")
            return plain_wav((work / _SPEECH).read_bytes())
    except subprocess.SubprocessError as error:
        raise OSError("ffmpeg could not synthesize a call") from error
