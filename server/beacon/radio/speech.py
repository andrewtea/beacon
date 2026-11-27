"""Synthesized speech: words said by ffmpeg's `flite` voice, as a WAV file.

It is what generated calls are made of when a paid Ember is to hear them
(`beacon.scale.generate`, and the invented story's calls in
`beacon.scenario.radio.SpokenShelf`): clean synthetic speech, which is no radio. The
words are Beacon's own, a script made from public records or the invented story's
lines, and no recording of anybody.

**No file is written.** The words go to ffmpeg in its arguments and the speech
comes back on a pipe, so nothing is left anywhere however the process ends. The
words are in the command line of that ffmpeg for as long as it runs, where anybody
on the machine who may list processes can read them: they are words Beacon made up
or public records, and nothing else is to be synthesized here.
"""

import os
import subprocess

from beacon.radio.audio import SAMPLE_RATE, pcm_to_wav

_TIMEOUT_SECONDS = 30
# The most words one synthesis takes: a call is a sentence or two, and an argument
# of a command has a length it cannot pass.
MAX_WORDS = 2_000


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

    Raises OSError when ffmpeg could not synthesize them, and ValueError for more
    than `MAX_WORDS` characters."""
    if len(words) > MAX_WORDS:
        raise ValueError("That is more than one synthesis takes.")
    try:
        done = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"flite=text={_in_a_filtergraph(words)}:voice=slt",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except subprocess.SubprocessError as error:
        raise OSError("ffmpeg could not synthesize a call") from error
    if done.returncode != 0 or not done.stdout:
        raise OSError("ffmpeg could not synthesize a call")
    return pcm_to_wav(done.stdout)


def _in_a_filtergraph(words: str) -> str:
    """The words as the value of a filter's option inside a filtergraph, so that
    ffmpeg reads them as the words and as nothing else: no character of them ends
    the option, the filter, or the graph.

    ffmpeg takes escaping off twice, once for the graph and once for the filter's
    options, so it is put on twice: for the option's value a backslash before a
    backslash, a quote, and a colon, and then for the graph a backslash before a
    backslash, a quote, a bracket, a comma, and a semicolon. With that, a text read
    this way was synthesized to the same samples as the same text read from a file
    (Nov 27, 2026, ffmpeg's flite, texts with every one of those characters)."""
    return _escaped(_escaped(words, "':"), "'[],;")


def _escaped(text: str, special: str) -> str:
    text = text.replace("\\", "\\\\")
    for each in special:
        text = text.replace(each, "\\" + each)
    return text
