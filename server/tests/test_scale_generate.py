"""Generated calls: what the script says of a day's records, and the directory a
run reads them from. The records are invented."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from beacon.ember import EmberClient, Storage
from beacon.evidence import EvidenceStore
from beacon.incident.places import Blocks
from beacon.ingest.recordings import GENERATED_MARK, Directory, RecordedRadio
from beacon.radio.audio import pcm_to_wav, plain_wav
from beacon.radio.vocabulary import Known
from beacon.scale import datasf, generate, lists
from tests.scale_day import FELL_GOUGH, GEARY_17TH, HAYES_GOUGH, ROSTER, call, centerlines

LISTS = lists.lists_of(centerlines(), ROSTER)
BLOCKS = Blocks.of(LISTS.blocks)
KNOWN = Known.of(streets=LISTS.streets, units=LISTS.units)
NOW = datetime(2026, 3, 10, tzinfo=UTC)

FIRE = call(
    "260620001",
    "Structure Fire / Smoke in Building",
    "08:01:30",
    HAYES_GOUGH,
    ("E36", "T06", "B02"),
    on_scene="08:05:00",
    available="09:10:00",
    alarms="2",
)
MEDICAL = call(
    "260620002",
    "Medical Incident",
    "09:00:00",
    FELL_GOUGH,
    ("M552", "E05"),
    on_scene="09:06:00",
    available="09:40:00",
)


def records_of(*calls: list[dict[str, Any]]) -> list[datasf.Record]:
    records, unreadable = datasf.read_records([row for rows in calls for row in rows])
    assert unreadable == 0
    return records


def test_a_call_is_scripted_as_its_dispatch_its_arrival_and_its_clearing() -> None:
    script, unplaced = generate.script_of(records_of(FIRE), BLOCKS)

    assert unplaced == 0
    dispatch, arrived, cleared = script
    assert [line.at.isoformat() for line in script] == [
        "2026-03-03T16:01:30+00:00",
        "2026-03-03T16:05:00+00:00",
        "2026-03-03T17:10:00+00:00",
    ]
    assert {line.number for line in script} == {"260620001"}
    # The listed block of one of the corner's streets that is nearest DataSF's point.
    assert dispatch.form == {
        "clarity": "clear",
        "category": "fire",
        "status": "dispatched",
        "details": [],
        "units": ["E36", "T6", "B2"],
        "alarm": 2,
        "street": "Gough St",
        "block": 300,
        "cross_street": "Hayes St",
    }
    assert (arrived.form["status"], arrived.form["units"], arrived.form["street"]) == (
        "on_scene",
        ["E36"],
        None,
    )
    assert (cleared.form["status"], cleared.form["units"]) == ("cleared", ["E36", "T6", "B2"])
    # The words a voice says hold what the form holds and no more.
    assert dispatch.words == (
        "Engine 36, Truck 6, Battalion 2. Respond to a fire, alarm level 2. "
        "300 block of Gough Street, cross of Hayes Street."
    )
    assert (arrived.words, cleared.words) == (
        "Engine 36, on scene at the fire.",
        "Engine 36, Truck 6, Battalion 2, clear of the fire and available.",
    )


def test_a_medical_call_is_scripted_as_datasf_lists_it_and_no_more() -> None:
    script, _ = generate.script_of(records_of(MEDICAL), BLOCKS)

    dispatch = script[0]
    assert (dispatch.form["category"], dispatch.form["alarm"], dispatch.form["details"]) == (
        "medical",
        None,
        [],
    )
    assert dispatch.words.startswith("Medic 552, Engine 5. Respond to a medical.")


def test_a_call_with_no_block_near_it_names_none_and_one_with_no_unit_is_not_scripted() -> None:
    far = ("HAYES ST/GOUGH ST", -122.4400, 37.7900)
    nowhere = call("260620003", "Alarms", "10:00:00", far, ("E36",))
    box = call("260620004", "Alarms", "10:30:00", ("CALL BOX: PIER 39", -122.41, 37.81), ("T06",))
    unnamed = call("260620005", "Other", "11:00:00", GEARY_17TH, ("SCRT1", "EMS6A"))

    script, unplaced = generate.script_of(records_of(nowhere, box, unnamed), BLOCKS)

    # Neither is given a block it is not near, or one of a street it is not on.
    assert unplaced == 2
    assert [(line.number, line.form["street"], line.form["block"]) for line in script] == [
        ("260620003", None, None),
        ("260620004", None, None),
    ]


@pytest.fixture
def written(tmp_path: Path) -> Path:
    script, unplaced = generate.script_of(records_of(FIRE, MEDICAL), BLOCKS)
    root = tmp_path / "calls"
    assert generate.write_calls(root, script, "tones", unplaced=unplaced) == generate.Written(
        calls=6, voice="tones", unplaced=0
    )
    return root


def test_generated_calls_are_marked_and_read_as_a_stand_ins_by_the_radio_poller(
    written: Path, tmp_path: Path
) -> None:
    assert (written / GENERATED_MARK).read_text().startswith("Generated audio. No radio.")
    day = written / "generated" / "2026" / "3" / "3"
    assert len(list(day.glob("*.wav"))) == len(list(day.glob("*.json"))) == 6
    # The medical call's three are on a talkgroup of their own, which is heard as
    # an EMS-only one.
    assert len(list(day.glob(f"{generate.EMS_TALKGROUP}-*.wav"))) == 3
    assert not list(written.glob("*.part"))
    shelf = Directory(written, generate.SYSTEM)
    assert shelf.generated is True
    script = generate.read_script(written)
    assert (script.voice, script.calls, script.unplaced) == ("tones", 6, 0)
    store = EvidenceStore(tmp_path / "evidence.sqlite3", "mock")
    poller = RecordedRadio(
        shelf,
        EmberClient.fake(Storage.under(tmp_path), script.hearer),
        talkgroups=(generate.TALKGROUP, generate.EMS_TALKGROUP),
        ems_talkgroups=(generate.EMS_TALKGROUP,),
        known=KNOWN,
        backlog=True,
    )

    report = poller.poll(store, NOW)

    assert (report.added, report.unreadable, report.partial) == (6, 0, False)
    rows = store.search()
    # Mock rows, whoever hears them, at the times the records give.
    assert {(row.origin, row.model, row.stand_in) for row in rows} == {("mock", "fake", True)}
    assert rows[0].occurred_at == datetime(2026, 3, 3, 16, 1, 30, tzinfo=UTC)
    assert (rows[0].place, rows[0].data["units"], rows[0].data["alarm"]) == (
        "300 block of Gough St",
        ["E36", "T6", "B2"],
        2,
    )
    # A medical call is a medical response with its crews and its block.
    medical = next(row for row in rows if row.data["event"] == "medical response")
    assert (medical.data["status"], medical.data["details"], medical.data["alarm"]) == (
        "unknown",
        [],
        None,
    )
    # And the live store takes none of it.
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    with pytest.raises(Exception, match="holds nothing a stand-in produced"):
        poller.poll(live, NOW)


def test_the_stand_in_makes_out_nothing_in_audio_that_is_not_in_the_script(written: Path) -> None:
    script = generate.read_script(written)

    def answer_for(audio: bytes) -> dict[str, Any]:
        body = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": base64.b64encode(audio).decode()},
                        }
                    ],
                }
            ]
        }
        (message,) = [item for item in script.hearer(body)["output"] if item["type"] == "message"]
        return json.loads(message["content"][0]["text"])

    first = sorted((written / "generated" / "2026" / "3" / "3").glob("*.wav"))[0]
    assert answer_for(first.read_bytes())["clarity"] == "clear"
    assert answer_for(b"RIFF another clip") == {
        "clarity": "unintelligible",
        "category": "unintelligible",
        "status": "unknown",
        "details": [],
        "units": [],
        "alarm": None,
        "street": None,
        "block": None,
        "cross_street": None,
    }


def test_calls_that_sound_the_same_say_the_same_so_a_medical_arrival_is_never_heard_as_a_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One engine is first at a fire and, an hour on, first at a medical call.
    fire = call("260620011", "Structure Fire", "08:00:00", HAYES_GOUGH, ("E36",),
                on_scene="08:05:00", alarms="1")  # fmt: skip
    medical = call("260620012", "Medical Incident", "09:00:00", FELL_GOUGH, ("E36",),
                   on_scene="09:06:00")  # fmt: skip
    script, _ = generate.script_of(records_of(fire, medical), BLOCKS)
    arrivals = [line for line in script if line.form["status"] == "on_scene"]
    assert [line.words for line in arrivals] == [
        "Engine 36, on scene at the fire.",
        "Engine 36, on scene at the medical.",
    ]
    # An alarm level of one is said, as a level of two is: a form with a level and
    # one with none do not sound the same.
    assert "alarm level 1" in script[0].words

    # Speech is the same audio for the same words.
    monkeypatch.setattr(generate, "can_speak", lambda: True)
    monkeypatch.setattr(
        generate,
        "_speech",
        lambda root, words: pcm_to_wav(hashlib.sha256(words.encode()).digest() * 1000),
    )
    root = tmp_path / "calls"
    generate.write_calls(root, script, "speech")
    hearer = generate.read_script(root).hearer
    heard = []
    for line in arrivals:
        audio = base64.b64encode(plain_wav(generate._speech(root, line.words))).decode()
        body = {"input": [{"content": [{"type": "input_audio", "input_audio": {"data": audio}}]}]}
        heard.append(json.loads(hearer(body)["output"][0]["content"][0]["text"])["category"])
    assert heard == ["fire", "medical"]

    # Were two calls that say different things ever to sound the same, nothing is written.
    monkeypatch.setattr(generate, "_speech", lambda root, words: pcm_to_wav(b"\x01\x00" * 16000))
    with pytest.raises(generate.GenerateError, match="could not be written"):
        generate.write_calls(tmp_path / "same", script, "speech")
    assert not (tmp_path / "same" / generate.SCRIPT_FILE).exists()


def test_generated_calls_are_written_only_into_a_directory_of_their_own(tmp_path: Path) -> None:
    script, _ = generate.script_of(records_of(FIRE), BLOCKS)
    recordings = tmp_path / "recordings"
    (recordings / "sfd").mkdir(parents=True)
    (recordings / "sfd" / "a-call.wav").write_bytes(b"somebody's recording")
    link = tmp_path / "link"
    link.symlink_to(recordings)

    for elsewhere in (recordings, link):
        with pytest.raises(generate.GenerateError):
            generate.write_calls(elsewhere, script, "tones")

    # Nothing was marked, changed, or removed there.
    assert sorted(path.name for path in recordings.rglob("*")) == ["a-call.wav", "sfd"]
    # A directory that is marked is written again, whole.
    root = tmp_path / "calls"
    generate.write_calls(root, script, "tones")
    generate.write_calls(root, script[:1], "tones")
    assert generate.read_script(root).calls == 1
    assert len(list(root.rglob("*.wav"))) == 1


def test_a_directory_with_no_mark_or_no_script_has_no_script_to_read(written: Path) -> None:
    (written / generate.SCRIPT_FILE).write_text("{")
    with pytest.raises(generate.GenerateError, match="script of generated calls cannot be read"):
        generate.read_script(written)
    (written / GENERATED_MARK).unlink()
    with pytest.raises(generate.GenerateError, match="is not marked BEACON-GENERATED"):
        generate.read_script(written)


def test_speech_is_asked_for_only_where_this_host_can_synthesize_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = generate.script_of(records_of(FIRE), BLOCKS)
    monkeypatch.setattr(generate, "can_speak", lambda: False)

    with pytest.raises(generate.GenerateError, match="Speech needs ffmpeg with the flite voice"):
        generate.write_calls(tmp_path / "calls", script, "speech")

    assert not (tmp_path / "calls").exists()
