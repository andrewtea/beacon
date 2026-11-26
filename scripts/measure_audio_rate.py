"""Measure the input tokens Muse Ember bills per second of audio.

    uv run --project server python scripts/measure_audio_rate.py [--clips DIR] [--budget USD]

Needs REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD for any clip with no
recorded answer. The budget ($0.05 by default) is the most the measurement may spend
over all its runs, not per run. The logic is in `beacon.ember.audio_rate`.
"""

import sys

from beacon.ember.audio_rate import main

if __name__ == "__main__":
    sys.exit(main())
