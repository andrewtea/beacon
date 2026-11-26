"""The audio rate measurement, against a stand-in that bills audio by its length."""

import base64
import io
import wave
from pathlib import Path
from typing import Any

import pytest

from beacon.ember import EmberClient, Storage, fake_response
from beacon.ember.audio_rate import (
    AudioRate,
    Measurement,
    main,
    measure,
    read_clips,
    report,
    tone_clip,
)

# What the stand-in bills: this much before any audio, and this much per second of it.
OVERHEAD_TOKENS = 110
TOKENS_PER_SECOND = 25


def bills_by_length(body: dict[str, Any]) -> dict[str, Any]:
    """Answers as Ember would if it billed audio by its length, reading the length
    from the WAV the request carries."""
    (message,) = body["input"]
    _text, audio = message["content"]
    assert audio["input_audio"]["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(audio["input_audio"]["data"])), "rb") as file:
        seconds = file.getnframes() / file.getframerate()
    return fake_response(
        text="ok", input_tokens=round(OVERHEAD_TOKENS + TOKENS_PER_SECOND * seconds)
    )


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


def test_the_rate_is_the_slope_of_billed_tokens_over_clip_length(storage: Storage) -> None:
    ember = EmberClient.fake(storage, bills_by_length)

    rate = measure(ember, [tone_clip(seconds) for seconds in (2, 3, 10)])

    assert [(made.name, made.seconds, made.input_tokens) for made in rate.measurements] == [
        ("tone-2s", 2.0, 160),
        ("tone-3s", 3.0, 185),
        ("tone-10s", 10.0, 360),
    ]
    # Not tokens divided by seconds, which the overhead would inflate to 80, 62 and 36.
    assert rate.tokens_per_second == pytest.approx(25)
    assert rate.overhead_tokens == pytest.approx(110)
    # 25 tokens a second for an hour is 90,000 tokens, at $0.10 per million.
    assert rate.usd_per_hour == pytest.approx(0.009)
    assert ember.spent_usd() == 0


def test_a_rate_needs_clips_of_two_lengths(storage: Storage) -> None:
    ember = EmberClient.fake(storage, bills_by_length)

    with pytest.raises(ValueError, match="two different lengths"):
        measure(ember, [tone_clip(5), tone_clip(5)])
    with pytest.raises(ValueError, match="two different lengths"):
        measure(ember, [])


def test_a_folder_of_wav_files_is_read_shortest_first_with_each_files_length(
    tmp_path: Path,
) -> None:
    (tmp_path / "a-long.wav").write_bytes(tone_clip(3).wav)
    (tmp_path / "b-short.wav").write_bytes(tone_clip(1).wav)
    (tmp_path / "notes.txt").write_text("not audio")

    clips = read_clips(tmp_path)

    assert [(clip.name, clip.seconds) for clip in clips] == [
        ("b-short.wav", 1.0),
        ("a-long.wav", 3.0),
    ]

    (tmp_path / "c-broken.wav").write_bytes(b"not a wav file")
    with pytest.raises(ValueError, match=r"c-broken\.wav is not a WAV file"):
        read_clips(tmp_path)


def test_the_report_says_when_a_stand_in_answered() -> None:
    def rate(source: str) -> AudioRate:
        made = Measurement("tone-2s", 2.0, 160, source, 0.000024)
        return AudioRate((made,), tokens_per_second=25.0, overhead_tokens=110.0)

    live = report(rate("live"))
    assert "Audio: 25.0 input tokens per second" in live
    assert "An hour of audio as input: $0.0090." in live
    assert "These calls spent $0.000024." in live
    assert "stand-in" not in live

    faked = report(rate("fake"))
    assert "A stand-in answered, not Ember" in faked
    assert "These calls spent $0.000000." in faked


def test_the_command_makes_no_call_without_a_key_and_says_what_it_needs(tmp_path: Path) -> None:
    out = io.StringIO()

    status = main([], env={"BEACON_DATA_DIR": str(tmp_path)}, out=out)

    assert status == 1
    assert "run again with REPLAY=0 and MODEL_API_KEY set" in out.getvalue()
    assert "at most $0.05" in out.getvalue()
    assert not (tmp_path / "ember" / "replay").exists()


@pytest.mark.parametrize(
    ("arguments", "env", "message"),
    [
        (["--budget", "lots"], {}, "'lots' is not a dollar amount"),
        ([], {"REPLAY": "0"}, "MODEL_API_KEY is not set"),
        ([], {"EMBER_STOP_USD": "three"}, "EMBER_STOP_USD"),
    ],
)
def test_the_command_reports_a_setting_it_cannot_use(
    tmp_path: Path, arguments: list[str], env: dict[str, str], message: str
) -> None:
    out = io.StringIO()

    status = main(arguments, env={"BEACON_DATA_DIR": str(tmp_path)} | env, out=out)

    assert status == 1
    assert out.getvalue().startswith("Not measured: ")
    assert message in out.getvalue()


def test_the_command_refuses_a_folder_with_fewer_than_two_lengths(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "only.wav").write_bytes(tone_clip(1).wav)
    out = io.StringIO()

    status = main(["--clips", str(clips)], env={"BEACON_DATA_DIR": str(tmp_path)}, out=out)

    assert status == 1
    assert "two different lengths" in out.getvalue()
