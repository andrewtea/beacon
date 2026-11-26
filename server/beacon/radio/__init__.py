"""Radio: what Beacon keeps of a dispatch radio call, and how Ember hears one.

    from beacon.radio import extract_transmission

    heard = extract_transmission(ember, wav, "wav")
    heard.event, heard.units, heard.place   # ("fire", ("E36", "T6"), "400 block of Hayes St")

See `vocabulary` for the closed vocabulary a call is kept in, `extract` for the Ember
call, and `audio` for how a call's audio gets to Ember without touching a disk.
"""

from beacon.radio.audio import AudioError, Ffmpeg, Transcoder
from beacon.radio.extract import ExtractionError, Transmission, extract_transmission, read_form
from beacon.radio.vocabulary import MEDICAL_EVENT

__all__ = [
    "MEDICAL_EVENT",
    "AudioError",
    "ExtractionError",
    "Ffmpeg",
    "Transcoder",
    "Transmission",
    "extract_transmission",
    "read_form",
]
