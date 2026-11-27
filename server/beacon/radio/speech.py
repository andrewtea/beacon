"""Synthesized speech: words said by ffmpeg's `flite` voice, as a WAV file.

It is what generated calls are made of when a paid Ember is to hear them
(`beacon.scale.generate`, and the invented story's calls in `beacon.scenario.recorder`):
clean synthetic speech, which is no radio. The words are Beacon's own, a script made
from public records or the invented story's lines, so the two files ffmpeg works on
here are no recording of anybody.
"""

import os
import subprocess
from pathlib import Path

from beacon.radio.audio import SAMPLE_RATE, plain_wav

_TIMEOUT_SECONDS = 30


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


def synthesize(root: Path, words: str) -> bytes:
    """The words as synthesized speech, a WAV file at Beacon's sample rate. `root`
    is a directory the two files ffmpeg works on are written into and deleted from.

    Raises OSError when ffmpeg could not synthesize them."""
    said, out = root / ".words.txt", root / ".speech.wav"
    try:
        said.write_text(words, encoding="utf-8")
        made = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"flite=textfile={said.name}:voice=slt",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                out.name,
            ],
            cwd=root,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
        if made.returncode != 0:
            raise OSError("ffmpeg could not synthesize a call")
        return plain_wav(out.read_bytes())
    except subprocess.SubprocessError as error:
        raise OSError("ffmpeg could not synthesize a call") from error
    finally:
        said.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
