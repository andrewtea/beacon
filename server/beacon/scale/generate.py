"""Generated calls for a scale run: a day of dispatch audio that is no radio.

There is no recording of San Francisco's dispatch radio that Beacon has the right to
use. So that the scale run can be built and run end to end before there is one, this
module writes a directory of calls in trunk-recorder's layout, **generated from
DataSF's own records of a day**: for each call DataSF lists, a dispatch at the time
it was dispatched, an arrival when DataSF says the first unit was on scene, and a
clearing when it says the last was available again.

The directory is marked `BEACON-GENERATED` at its top, so everything that reads it
(`beacon.ingest.recordings`) stores its rows as `mock` ones, whoever hears them.

**What a run over these calls can and cannot show.** The calls say what the records
say, at the times the records give, and the run is then scored against the same
records. So such a run measures whether the pipeline keeps what it is given: that
calls are heard once, grouped into incidents, placed on the map, and matched. It
does not measure Beacon's hearing of a real city's radio, how soon the radio tells
of a call, or what real traffic costs: real radio is noisy, clipped, and says things
no record holds. `beacon.scale.results` says so beside every number.

Two kinds of audio (`Voice`):

- `tones`: each call is a tone of its own, as the mock scenario's calls are. A tone
  says nothing, so only the stand-in for Ember "hears" it, by looking the call up in
  the script this module writes (`script.json`). Its hearing is right by
  construction.
- `speech`: each call is synthesized speech of the script's words (ffmpeg's `flite`
  voice). This is what a paid Ember is given when a key exists and recordings do
  not: clean synthetic speech, which is not radio either.

A medical call is generated as DataSF lists it: its units, the block nearest its
intersection, and the word "medical". DataSF publishes no more about it, and the
row Beacon keeps of it is a medical response with its crews and block, as for radio.
Medical calls are on a talkgroup of their own (`EMS_TALKGROUP`), which a run hears
as an EMS-only one, so that is what is kept whatever a hearer made of the audio.
"""

import base64
import hashlib
import json
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from beacon.ember import fake_response
from beacon.evidence import Point
from beacon.incident.places import Blocks
from beacon.ingest.recordings import GENERATED_MARK
from beacon.radio.audio import SAMPLE_RATE, AudioError, pcm_to_wav, plain_wav, wav_seconds
from beacon.radio.speech import can_speak
from beacon.radio.speech import synthesize as _speech
from beacon.radio.vocabulary import MAX_UNITS
from beacon.scale.datasf import Record

Voice = Literal["tones", "speech"]

# What the generated recorder calls its system, its one talkgroup, and its frequency.
SYSTEM = "generated"
TALKGROUP = 1
# Medical calls are generated on a talkgroup of their own, which a run hears as an
# EMS-only one: every call on it is kept as a medical response whatever its hearer
# made of it, as for a real EMS channel, so a paid Ember that mishears generated
# speech cannot have a medical call kept as anything else.
EMS_TALKGROUP = 2
_FREQUENCY = 851000000
SCRIPT_FILE = "script.json"
_MARK_TEXT = (
    "Generated audio. No radio.\n"
    "Made by Beacon's scale run (beacon.scale.generate) from DataSF's dispatch "
    "records of one day.\n"
)
# How far from DataSF's point a listed block of one of its streets may be and still
# be taken for the block of the call.
BLOCK_REACH_M = 250.0
# A tone plays this long, and this much longer for each unit it names.
_TONE_SECONDS = 2.0
_TONE_SECONDS_A_UNIT = 0.4
_MAX_TONE_SECONDS = 8.0

_UNIT_WORDS = {"E": "Engine", "T": "Truck", "B": "Battalion", "D": "Division", "M": "Medic"}
_KIND_WORDS = {
    "fire": "fire",
    "medical": "medical",
    "rescue": "rescue",
    "hazmat": "hazardous materials",
    "collision": "traffic collision",
    "alarm": "alarm sounding",
    "utility": "utility hazard",
    "service": "service call",
    "other": "incident",
}
_STREET_WORDS = {
    "St": "Street",
    "Ave": "Avenue",
    "Blvd": "Boulevard",
    "Dr": "Drive",
    "Ct": "Court",
    "Pl": "Place",
    "Ter": "Terrace",
    "Ln": "Lane",
    "Rd": "Road",
    "Hwy": "Highway",
    "Pkwy": "Parkway",
}

# What the stand-in answers for audio that is not in the script.
_NOTHING = {
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


class GenerateError(Exception):
    """Generated calls could not be written, or read back."""


@dataclass(frozen=True)
class Line:
    """One call of the script: what is said, when, and about which record."""

    # The record it was made from, by DataSF's call number.
    number: str
    at: datetime
    # What the call says, as the form `extract_transmission` fills in.
    form: Mapping[str, Any]
    # The same in words, for a voice to say.
    words: str


@dataclass(frozen=True)
class Written:
    """What `write_calls` wrote."""

    calls: int
    voice: Voice
    # Records whose intersection has no listed block near it: their calls name
    # units and no place, so their incidents have no point.
    unplaced: int


def script_of(records: Iterable[Record], blocks: Blocks) -> tuple[list[Line], int]:
    """The calls for a day's records, oldest first, and how many records could not
    be given a block: neither street of their intersection has a listed block within
    `BLOCK_REACH_M` of DataSF's point."""
    by_street: dict[str, list[tuple[int, Point]]] = {}
    for (street, block), point in blocks.points.items():
        by_street.setdefault(street, []).append((block, point))
    lines: list[Line] = []
    unplaced = 0
    for record in records:
        units = list(record.units[:MAX_UNITS])
        if not units:
            # A call that names no unit says nothing a later call could be tied to.
            continue
        place = _place(record, by_street)
        unplaced += place is None
        street, block, cross = place if place is not None else (None, None, None)
        dispatch = _form(
            record.kind,
            "dispatched",
            units=units,
            alarm=record.alarms if record.kind == "fire" else None,
            street=street,
            block=block,
            cross_street=cross,
        )
        lines.append(Line(record.number, record.dispatched_at, dispatch, _words(dispatch)))
        last = record.dispatched_at
        if record.on_scene_at is not None and record.on_scene_at > last:
            arrived = _form(record.kind, "on_scene", units=units[:1])
            lines.append(Line(record.number, record.on_scene_at, arrived, _words(arrived)))
            last = record.on_scene_at
        if record.cleared_at is not None and record.cleared_at > last:
            cleared = _form(record.kind, "cleared", units=units)
            lines.append(Line(record.number, record.cleared_at, cleared, _words(cleared)))
    lines.sort(key=lambda line: (line.at, line.number))
    return lines, unplaced


def write_calls(root: Path, lines: Sequence[Line], voice: Voice, *, unplaced: int = 0) -> Written:
    """Write the script's calls into `root` as trunk-recorder would leave them, with
    the mark that says they are generated and the script a stand-in hears them by.

    `root` is made when it is not there. A directory that holds anything and is not
    marked as generated is not written into.

    Raises GenerateError when the directory is somebody else's, when speech is asked
    for and this host's ffmpeg has no `flite` voice, or when a file cannot be written."""
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise GenerateError("Generated calls are written into a directory, not a link to one.")
    mark = root / GENERATED_MARK
    if root.is_dir() and any(root.iterdir()) and not (mark.is_file() and not mark.is_symlink()):
        raise GenerateError(
            f"The directory holds files and is not marked {GENERATED_MARK}: generated "
            "calls are not written among anything else."
        )
    if voice == "speech" and not can_speak():
        raise GenerateError(
            "Speech needs ffmpeg with the flite voice on this host. Tones need nothing, "
            "and only the stand-in for Ember hears them."
        )
    try:
        root.mkdir(parents=True, exist_ok=True)
        # The mark first: nothing generated is ever in a directory without it.
        mark.write_text(_MARK_TEXT, encoding="utf-8")
        # A script of an earlier writing is not left beside calls it does not describe.
        (root / SCRIPT_FILE).unlink(missing_ok=True)
        shutil.rmtree(root / SYSTEM, ignore_errors=True)
        heard: dict[str, Any] = {}
        for index, line in enumerate(lines):
            audio = _tone(index, line) if voice == "tones" else _speech(root, line.words)
            talkgroup = _talkgroup(line)
            stem = _stem(index, line.at, talkgroup)
            day = root.joinpath(SYSTEM, str(line.at.year), str(line.at.month), str(line.at.day))
            day.mkdir(parents=True, exist_ok=True)
            # The audio before the JSON, so that a reader never finds a call whose
            # JSON is there and whose audio is not.
            (day / f"{stem}.wav").write_bytes(audio)
            seconds = wav_seconds(audio)
            if seconds is None:
                raise AudioError("A generated call is no WAV file.")
            (day / f"{stem}.json").write_text(
                json.dumps(_details(line.at, seconds, talkgroup)), encoding="utf-8"
            )
            digest = hashlib.sha256(plain_wav(audio)).hexdigest()
            known = heard.setdefault(digest, {"number": line.number, "form": dict(line.form)})
            if known["form"] != dict(line.form):
                # The stand-in finds a call's form by its audio. Two calls that sound
                # the same and say different things would be given one form.
                raise AudioError("Two generated calls sound the same and say different things.")
        script = {
            "what": "Generated audio. No radio.",
            "voice": voice,
            "calls": len(lines),
            "unplaced": unplaced,
            "heard": heard,
        }
        # The script is written last and whole: it is what says the calls are all
        # there, so a writing that was cut short is begun again and never read.
        beside = root / (SCRIPT_FILE + ".part")
        beside.write_text(json.dumps(script), encoding="utf-8")
        os.replace(beside, root / SCRIPT_FILE)
    except (OSError, AudioError) as error:
        raise GenerateError("The generated calls could not be written.") from error
    return Written(calls=len(lines), voice=voice, unplaced=unplaced)


@dataclass(frozen=True)
class Script:
    """The script of a directory of generated calls, read back."""

    voice: Voice
    calls: int
    unplaced: int
    # What each call says, and a record it was made from, by the digest of its
    # audio as Ember is sent it. Calls that sound the same say the same.
    heard: Mapping[str, Mapping[str, Any]]

    def hearer(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """A responder for `EmberClient.fake` that answers `extract_transmission`
        for the script's calls: it finds the audio in the request and fills in the
        form the script has for it. Any other audio is unintelligible to it. It
        hears nothing: its answers are the script's, and a stand-in's."""
        form: Mapping[str, Any] = _NOTHING
        for item in body.get("input") or []:
            for part in item.get("content") or [] if isinstance(item, Mapping) else []:
                audio = part.get("input_audio") if isinstance(part, Mapping) else None
                if isinstance(audio, Mapping) and isinstance(audio.get("data"), str):
                    digest = hashlib.sha256(base64.b64decode(audio["data"])).hexdigest()
                    known = self.heard.get(digest)
                    form = _NOTHING if known is None else known["form"]
        return fake_response(text=json.dumps(form))


def read_script(root: Path) -> Script:
    """The script `write_calls` left in a directory of generated calls.

    Raises GenerateError when the directory is not marked as generated, or its
    script is not there or cannot be read."""
    mark = root / GENERATED_MARK
    if mark.is_symlink() or not mark.is_file():
        raise GenerateError(f"The directory is not marked {GENERATED_MARK}.")
    try:
        said = json.loads((root / SCRIPT_FILE).read_text(encoding="utf-8"))
        voice, heard = said["voice"], said["heard"]
        if voice not in ("tones", "speech") or not isinstance(heard, dict):
            raise ValueError
        for known in heard.values():
            if not isinstance(known["form"], dict) or not isinstance(known["number"], str):
                raise ValueError
        return Script(
            voice=voice, calls=int(said["calls"]), unplaced=int(said["unplaced"]), heard=heard
        )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise GenerateError("The directory's script of generated calls cannot be read.")


def _place(
    record: Record, by_street: Mapping[str, list[tuple[int, Point]]]
) -> tuple[str, int, str] | None:
    """The listed block of one of the record's streets that is nearest DataSF's
    point for it, with the other street as its cross street."""
    if record.point is None or len(record.streets) != 2:
        return None
    best: tuple[float, str, int, str] | None = None
    for street, cross in (record.streets, record.streets[::-1]):
        for block, point in by_street.get(street, ()):
            away = record.point.distance_m(point)
            if away <= BLOCK_REACH_M and (best is None or (away, street, block) < best[:3]):
                best = (away, street, block, cross)
    return None if best is None else (best[1], best[2], best[3])


def _form(kind: str, status: str, **fields: Any) -> dict[str, Any]:
    return {
        "clarity": "clear",
        "category": kind,
        "status": status,
        "details": [],
        "units": [],
        "alarm": None,
        "street": None,
        "block": None,
        "cross_street": None,
    } | fields


def _words(form: Mapping[str, Any]) -> str:
    """A form as a dispatcher might say it, for a voice. Nothing is in the words
    that is not in the form, **and everything in the form is in the words**: two
    forms that differ are said differently, so that calls which sound the same say
    the same, and the stand-in, which finds a call's form by its audio, never gives
    one call another's. So an arrival and a clearing name their kind of call, as a
    real one would not."""
    units = ", ".join(_unit_words(unit) for unit in form["units"])
    kind = _KIND_WORDS.get(form["category"], "incident")
    if form["status"] == "on_scene":
        return f"{units}, on scene at the {kind}."
    if form["status"] == "cleared":
        return f"{units}, clear of the {kind} and available."
    said = f"{units}. Respond to a {kind}"
    if form["alarm"] is not None:
        said += f", alarm level {form['alarm']}"
    if form["street"] is not None:
        said += f". {form['block']} block of {_street_words(form['street'])}"
        if form["cross_street"] is not None:
            said += f", cross of {_street_words(form['cross_street'])}"
    return said + "."


def _unit_words(unit: str) -> str:
    letters = unit.rstrip("0123456789")
    number = unit[len(letters) :]
    return f"{_UNIT_WORDS.get(letters, ' '.join(letters))} {number}"


def _street_words(street: str) -> str:
    words = street.split()
    return " ".join([*words[:-1], _STREET_WORDS.get(words[-1], words[-1])])


def _talkgroup(line: Line) -> int:
    return EMS_TALKGROUP if line.form["category"] == "medical" else TALKGROUP


def _stem(index: int, at: datetime, talkgroup: int) -> str:
    at = at.astimezone(UTC)
    return (
        f"{talkgroup}-{int(at.timestamp())}.{at.microsecond // 1000:03d}_{_FREQUENCY}-call_{index}"
    )


def _details(at: datetime, seconds: float, talkgroup: int) -> dict[str, Any]:
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
        "talkgroup": talkgroup,
        "talkgroup_tag": "Generated dispatch",
        "audio_type": "digital",
        "short_name": SYSTEM,
    }


def _tone(index: int, line: Line) -> bytes:
    """A tone for a call, at a pitch and of a length of its own among its
    neighbours, so that no two calls of a day have the same audio."""
    seconds = min(_TONE_SECONDS + _TONE_SECONDS_A_UNIT * len(line.form["units"]), _MAX_TONE_SECONDS)
    # A square wave, a period of which is a number of samples of its own.
    half = 20 + index % 60
    period = (6000).to_bytes(2, "little", signed=True) * half + (-6000).to_bytes(
        2, "little", signed=True
    ) * half
    wanted = 2 * round(seconds * SAMPLE_RATE)
    # The call's number in the day is written into the first samples, which makes
    # the audio of two calls differ whatever their pitch and length.
    samples = index.to_bytes(4, "little") + period * (wanted // len(period) + 1)
    return pcm_to_wav(samples[:wanted])
