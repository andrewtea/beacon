"""Measure how many input tokens Muse Ember bills for a second of audio.

The plan's budget guesses 25 to 30 tokens per second and says nobody has checked it.
This sends clips of known length through the Ember client, reads the input tokens
each answer was billed for, and fits a line through them: the slope is the rate, and
the intercept is what a request costs before any audio.

    export REPLAY=0 MODEL_API_KEY=... EMBER_SPENT_BEFORE_USD=...
    uv run --project server python scripts/measure_audio_rate.py

The clips are generated tones unless `--clips` names a folder of WAV files. A tone is
not speech: if Ember bills audio by its length the rate is the same, and if it does
not, only recordings of the radio traffic Beacon will send give the real number.

Every call goes through `EmberClient`, so it is in the ledger and held by the spending
stop and by a scope of its own. The scope is the same every time the command runs, so
`--budget` is the most the measurement may spend over all its runs, not per run.

The tones' answers are recorded, so running the command again replays them and costs
nothing. Clips from a folder can be recordings of radio traffic, so their calls are
made as sensitive ones: nothing of them is stored but the accounting, and a rerun is
paid for again, under the same budget.
"""

import argparse
import base64
import io
import os
import sys
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from beacon.ember.client import MIN_OUTPUT_TOKENS, EmberClient, SpendScope
from beacon.ember.errors import EmberError, ReplayMissError
from beacon.ember.pricing import STANDARD, parse_usd, to_usd

TASK = "measure_audio_rate"
SCOPE = "measure-audio-rate"
# Ten calls, as the plan asks for. Short clips first, so a rate far above the guess
# shows in the cheapest calls.
TONE_SECONDS = (2, 4, 6, 8, 10, 15, 20, 30, 45, 60)
DEFAULT_BUDGET = "0.05"
PROMPT = "Reply with the single word ok."

_SAMPLE_RATE = 16_000
# 400 Hz at 16 kHz repeats every 40 samples: a square wave at a quarter of full scale.
_PERIOD = (8192).to_bytes(2, "little", signed=True) * 20 + (-8192).to_bytes(
    2, "little", signed=True
) * 20


@dataclass(frozen=True)
class Clip:
    name: str
    seconds: float
    # A whole WAV file.
    wav: bytes


@dataclass(frozen=True)
class Measurement:
    name: str
    seconds: float
    # What the answer's `usage` reported, which is what Meta bills.
    input_tokens: int
    source: str
    cost_usd: float


@dataclass(frozen=True)
class AudioRate:
    measurements: tuple[Measurement, ...]
    # The least-squares line through (seconds, input tokens).
    tokens_per_second: float
    overhead_tokens: float

    @property
    def usd_per_hour(self) -> float:
        """What an hour of audio costs as input at the standard rate."""
        return self.tokens_per_second * 3600 * to_usd(STANDARD.input)


def tone_clip(seconds: int) -> Clip:
    """A mono 16 kHz tone of a whole number of seconds."""
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1:
        raise ValueError(f"A tone lasts a whole number of seconds, not {seconds!r}.")
    file = io.BytesIO()
    with wave.open(file, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(_SAMPLE_RATE)
        out.writeframes(_PERIOD * (seconds * _SAMPLE_RATE // 40))
    return Clip(f"tone-{seconds}s", float(seconds), file.getvalue())


def read_clip(path: Path) -> Clip:
    """A WAV file and its length. Raises ValueError when it is not one, holds no
    audio, or is cut short of the length its header declares."""
    data = path.read_bytes()
    try:
        with wave.open(io.BytesIO(data), "rb") as file:
            declared, rate = file.getnframes(), file.getframerate()
            frame_bytes = file.getnchannels() * file.getsampwidth()
            held = len(file.readframes(declared))
    except (wave.Error, EOFError) as error:
        raise ValueError(f"{path.name} is not a WAV file: {error}") from error
    if declared <= 0 or rate <= 0 or frame_bytes <= 0:
        raise ValueError(f"{path.name} holds no audio.")
    if held != declared * frame_bytes:
        # The length would be the header's and the audio sent would be shorter, which
        # puts a wrong point on the line.
        raise ValueError(
            f"{path.name} is cut short: its header declares {declared:,} frames and it "
            f"holds {held // frame_bytes:,}."
        )
    return Clip(path.name, declared / rate, data)


def read_clips(directory: Path) -> list[Clip]:
    """Every `.wav` file in a folder, shortest first."""
    clips = [read_clip(path) for path in sorted(directory.glob("*.wav"))]
    return sorted(clips, key=lambda clip: clip.seconds)


def measure(ember: EmberClient, clips: Sequence[Clip], *, sensitive: bool) -> AudioRate:
    """One Ember call per clip, and the line through what they were billed.
    `sensitive` is `EmberClient.respond`'s: True for clips that can hold radio traffic.

    Raises ValueError unless the clips have at least two different lengths, which a
    line needs. Raises what `EmberClient.respond` raises."""
    if len({clip.seconds for clip in clips}) < 2:
        raise ValueError("Measuring a rate needs clips of at least two different lengths.")
    measurements = []
    for clip in clips:
        answer = ember.respond(
            task=TASK,
            effort="minimal",
            # The answer is not read, so it gets the smallest output limit the API takes.
            max_output_tokens=MIN_OUTPUT_TOKENS,
            sensitive=sensitive,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": PROMPT},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(clip.wav).decode("ascii"),
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
        )
        measurements.append(
            Measurement(
                name=clip.name,
                seconds=clip.seconds,
                input_tokens=answer.usage.input_tokens,
                source=answer.source,
                cost_usd=answer.cost_usd,
            )
        )
    slope, intercept = _fit([(made.seconds, made.input_tokens) for made in measurements])
    return AudioRate(tuple(measurements), tokens_per_second=slope, overhead_tokens=intercept)


def _fit(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """The slope and intercept of the least-squares line through the points."""
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return slope, mean_y - slope * mean_x


def report(rate: AudioRate) -> str:
    lines = [f"{'clip':<24}{'seconds':>9}{'input tokens':>14}  source"]
    lines += [
        f"{made.name[:23]:<24}{made.seconds:>9.1f}{made.input_tokens:>14,}  {made.source}"
        for made in rate.measurements
    ]
    live = sum(made.cost_usd for made in rate.measurements if made.source == "live")
    lines += [
        "",
        f"Audio: {rate.tokens_per_second:.1f} input tokens per second (the plan guessed 25 to 30).",
        f"Before any audio: {rate.overhead_tokens:.0f} input tokens per request.",
        f"An hour of audio as input: ${rate.usd_per_hour:.4f}.",
        f"These calls spent ${live:.6f}.",
    ]
    if any(made.source == "fake" for made in rate.measurements):
        lines.append("A stand-in answered, not Ember: these numbers say nothing about Ember.")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    """The command. Returns its exit status: 0 when the rate was measured."""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(
        prog="measure_audio_rate.py",
        description="Measure the input tokens Muse Ember bills per second of audio.",
    )
    parser.add_argument(
        "--clips",
        type=Path,
        help="a folder of WAV files to send in place of generated tones; "
        "send only recordings you have the rights to send",
    )
    parser.add_argument(
        "--budget",
        default=DEFAULT_BUDGET,
        help="the most the measurement may spend over all its runs, in dollars "
        f"(default {DEFAULT_BUDGET})",
    )
    options = parser.parse_args(argv)
    try:
        budget = parse_usd(options.budget)
        clips = (
            [tone_clip(seconds) for seconds in TONE_SECONDS]
            if options.clips is None
            else read_clips(options.clips)
        )
        ember = EmberClient.from_env(env, scope=SpendScope(SCOPE, budget))
        rate = measure(ember, clips, sensitive=options.clips is not None)
    except ReplayMissError:
        rerun = (
            "the tones' answers are recorded, so a rerun costs nothing"
            if options.clips is None
            else "nothing of a folder's clips is recorded, so a rerun is paid for again"
        )
        print(
            "No recorded measurement to replay. Measuring needs real Ember calls: run "
            "again with REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD set. The "
            f"measurement spends at most ${options.budget} over all its runs, and {rerun}.",
            file=out,
        )
        return 1
    except (EmberError, ValueError, OSError) as error:
        print(f"Not measured: {error}", file=out)
        return 1
    print(report(rate), file=out)
    return 0
