"""The audio rate measurement, against a stand-in that bills audio by its length."""

import base64
import io
import json
import sqlite3
import wave
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import EmberClient, SpendScope, Storage, fake_response
from beacon.ember.audio_rate import (
    AudioRate,
    Measurement,
    main,
    measure,
    read_clip,
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

    rate = measure(ember, [tone_clip(seconds) for seconds in (2, 3, 10)], sensitive=False)

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
        measure(ember, [tone_clip(5), tone_clip(5)], sensitive=False)
    with pytest.raises(ValueError, match="two different lengths"):
        measure(ember, [], sensitive=False)


def test_clips_that_can_hold_radio_traffic_leave_nothing_of_the_answer_on_disk(
    storage: Storage,
) -> None:
    def transcribes(body: dict[str, Any]) -> dict[str, Any]:
        return bills_by_length(body) | {
            "output": fake_response(text="Medic 52, 1458 Hayes Street")["output"]
        }

    rate = measure(
        EmberClient.fake(storage, transcribes), [tone_clip(1), tone_clip(2)], sensitive=True
    )

    assert rate.tokens_per_second == pytest.approx(25)
    with sqlite3.connect(storage.ledger_db) as db:
        rows = db.execute("SELECT request, response, input_tokens FROM ember_calls").fetchall()
    assert [tokens for _request, _response, tokens in rows] == [135, 160]
    assert "Hayes" not in json.dumps(rows)
    assert all(json.loads(request)["withheld"] == "sensitive" for request, _, _ in rows)


def test_a_clip_cut_short_of_its_declared_length_is_refused(tmp_path: Path) -> None:
    whole = tone_clip(2).wav
    (tmp_path / "whole.wav").write_bytes(whole)
    assert read_clip(tmp_path / "whole.wav").seconds == 2.0

    # The header still declares two seconds, and half of the audio is gone.
    (tmp_path / "cut.wav").write_bytes(whole[: len(whole) // 2])
    with pytest.raises(ValueError, match=r"cut\.wav is cut short: its header declares"):
        read_clip(tmp_path / "cut.wav")
    # One byte short is short.
    (tmp_path / "cut.wav").write_bytes(whole[:-1])
    with pytest.raises(ValueError, match="cut short"):
        read_clips(tmp_path)


def test_a_folder_of_wav_files_is_read_shortest_first_with_each_files_length(
    tmp_path: Path,
) -> None:
    (tmp_path / "a-long.wav").write_bytes(tone_clip(3).wav)
    (tmp_path / "b-short.wav").write_bytes(tone_clip(1).wav)
    (tmp_path / "notes.txt").write_text("not audio")

    clips = read_clips(tmp_path)

    # Named for their place by file name, and not by the file names themselves.
    assert [(clip.name, clip.seconds) for clip in clips] == [("clip-2", 1.0), ("clip-1", 3.0)]

    (tmp_path / "c-broken.wav").write_bytes(b"not a wav file")
    with pytest.raises(ValueError, match=r"^clip-3 is not a WAV file"):
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
    assert "REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD set" in out.getvalue()
    assert "at most $0.05 over all its runs" in out.getvalue()
    assert "the tones' answers are recorded, so a rerun costs nothing" in out.getvalue()
    assert not (tmp_path / "ember" / "replay").exists()


def test_the_command_says_a_folders_clips_are_paid_for_on_every_run(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    for seconds in (1, 2):
        (clips / f"{seconds}.wav").write_bytes(tone_clip(seconds).wav)
    out = io.StringIO()

    status = main(["--clips", str(clips)], env={"BEACON_DATA_DIR": str(tmp_path)}, out=out)

    assert status == 1
    assert "nothing of a folder's clips is recorded, so a rerun is paid for again" in (
        out.getvalue()
    )


@pytest.mark.parametrize(("from_folder", "kept"), [(True, False), (False, True)])
def test_the_command_stores_the_answers_about_tones_and_not_about_a_folders_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_folder: bool, kept: bool
) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    for seconds in (1, 2):
        (clips / f"{seconds}.wav").write_bytes(tone_clip(seconds).wav)
    storage = Storage.under(tmp_path / "data")

    def transcribes(body: dict[str, Any]) -> dict[str, Any]:
        return bills_by_length(body) | {"output": fake_response(text="1458 Hayes")["output"]}

    # The command's client is replaced by a stand-in, which answers with what a model
    # could say about radio traffic.
    monkeypatch.setattr(
        EmberClient,
        "from_env",
        classmethod(lambda cls, env, scope: EmberClient.fake(storage, transcribes, scope=scope)),
    )

    status = main(["--clips", str(clips)] if from_folder else [], env={}, out=io.StringIO())

    assert status == 0
    with sqlite3.connect(storage.ledger_db) as db:
        stored = json.dumps(db.execute("SELECT request, response FROM ember_calls").fetchall())
    assert ("Hayes" in stored) == kept


@pytest.mark.parametrize(
    ("arguments", "env", "message"),
    [
        (["--budget", "lots"], {}, "'lots' is not a dollar amount"),
        ([], {"REPLAY": "0"}, "MODEL_API_KEY is not set"),
        (
            [],
            {"REPLAY": "0", "MODEL_API_KEY": "test-key"},
            "EMBER_SPENT_BEFORE_USD is not set",
        ),
        ([], {"BEACON_DATA_DIR": "data"}, "BEACON_DATA_DIR must be an absolute path"),
        ([], {"EMBER_STOP_USD": "three"}, "EMBER_STOP_USD"),
        ([], {"EMBER_STOP_USD": "9" * 320}, "EMBER_STOP_USD: '9999"),
        (["--budget", "9" * 320], {}, "too large to be a dollar amount"),
    ],
)
def test_the_command_reports_a_setting_it_cannot_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    env: dict[str, str],
    message: str,
) -> None:
    # The spending settings are the process's, whatever mapping the command is given.
    for name in [name for name in env if name.startswith("EMBER_")]:
        monkeypatch.setenv(name, env.pop(name))
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


def _named_clips(tmp_path: Path) -> Path:
    """A folder named for an address, of recordings named for a patient."""
    clips = tmp_path / "1458-hayes-street"
    clips.mkdir()
    for seconds in (1, 2):
        (clips / f"jane-doe-{seconds}.wav").write_bytes(tone_clip(seconds).wav)
    return clips


def _rejects_by_name(status: int) -> httpx2.Response:
    error = {"message": "Bad audio: 'Jane Doe, 1458 Hayes'", "type": "JaneDoe", "code": "hayes"}
    return httpx2.Response(status, json={"error": error})


@pytest.mark.parametrize("failing", ["count", "create", None])
def test_nothing_the_command_prints_about_a_folders_clips_says_what_is_in_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str | None
) -> None:
    clips = _named_clips(tmp_path)
    storage = Storage.under(tmp_path / "data")
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")

    def api(request: httpx2.Request) -> httpx2.Response:
        counting = request.url.path == "/v1/responses/input_tokens"
        if failing == ("count" if counting else "create"):
            return _rejects_by_name(400)
        if counting:
            return httpx2.Response(200, json={"input_tokens": 1200})
        usage = {"input_tokens": 1200, "output_tokens": 16, "total_tokens": 1216}
        said = [{"type": "message", "content": [{"type": "output_text", "text": "Jane Doe"}]}]
        return httpx2.Response(200, json={"status": "completed", "output": said, "usage": usage})

    def live(cls: type[EmberClient], env: object, scope: SpendScope) -> EmberClient:
        http_client = httpx2.Client(transport=httpx2.MockTransport(api))
        return cls.live(storage, api_key="test-key", http_client=http_client, scope=scope)

    monkeypatch.setattr(EmberClient, "from_env", classmethod(live))
    out = io.StringIO()

    status = main(["--clips", str(clips)], env={}, out=out)

    printed = out.getvalue()
    assert status == (1 if failing else 0)
    for word in ("jane", "doe", "hayes", "1458"):
        assert word not in printed.lower()
    assert "Clips are numbered in the order of their file names" in printed
    if failing:
        assert "HTTP 400" in printed
    else:
        assert "clip-1" in printed
        assert "clip-2" in printed
    stored = (
        sqlite3.connect(storage.ledger_db).execute("SELECT * FROM ember_calls").fetchall()
        if storage.ledger_db.exists()
        else []
    )
    assert "jane" not in json.dumps(stored).lower()


def test_a_file_or_folder_the_command_cannot_read_is_not_named_in_what_it_prints(
    tmp_path: Path,
) -> None:
    clips = _named_clips(tmp_path)
    (clips / "jane-doe-0.wav").write_bytes(b"not a wav file")
    out = io.StringIO()

    assert main(["--clips", str(clips)], env={"BEACON_DATA_DIR": str(tmp_path)}, out=out) == 1

    assert out.getvalue().startswith("Not measured: clip-1 is not a WAV file")
    assert "Clips are numbered in the order of their file names" in out.getvalue()
    assert "jane" not in out.getvalue().lower()

    # A file that is a folder cannot be read at all, which is the system's error.
    (clips / "jane-doe-0.wav").unlink()
    (clips / "jane-doe-0.wav").mkdir()
    out = io.StringIO()
    assert main(["--clips", str(clips)], env={"BEACON_DATA_DIR": str(tmp_path)}, out=out) == 1
    assert out.getvalue() == "Not measured: Is a directory\n"
