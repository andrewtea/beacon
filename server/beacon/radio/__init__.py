"""Radio: what Beacon keeps of a dispatch radio call, and how Ember hears one.

    from beacon.radio import Known, extract_transmission

    known = Known.of(streets=["Hayes St"], units=["E36", "T6"])
    heard = extract_transmission(ember, wav, "wav", known=known)
    heard.event, heard.units, heard.place   # ("fire", ("E36", "T6"), "400 block of Hayes St")

See `vocabulary` for the closed vocabulary a call is kept in and the lists a unit or
a street has to be on, `extract` for the Ember call, and `audio` for how a call's
audio gets to Ember without touching a disk.
"""

from beacon.radio.audio import (
    AudioError,
    ConversionStopped,
    Ffmpeg,
    Transcoder,
    core_dump_problem,
    forbid_core_dumps,
    plain_wav,
)
from beacon.radio.extract import ExtractionError, Transmission, extract_transmission, read_form
from beacon.radio.vocabulary import MEDICAL_EVENT, NOTHING_KNOWN, Known

__all__ = [
    "MEDICAL_EVENT",
    "NOTHING_KNOWN",
    "AudioError",
    "ConversionStopped",
    "ExtractionError",
    "Ffmpeg",
    "Known",
    "Transcoder",
    "Transmission",
    "core_dump_problem",
    "extract_transmission",
    "forbid_core_dumps",
    "plain_wav",
    "read_form",
]
