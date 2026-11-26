"""Measure the input tokens Muse Ember bills per second of audio.

    uv run --project server python scripts/measure_audio_rate.py [--clips DIR] [--budget USD]

Needs REPLAY=0 and MODEL_API_KEY the first time, and spends at most the budget
($0.05 by default). The logic is in `beacon.ember.audio_rate`.
"""

import sys

from beacon.ember.audio_rate import main

if __name__ == "__main__":
    sys.exit(main())
