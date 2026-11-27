"""The scale run: a day of dispatch calls through the pipeline, scored against
DataSF's record of the day.

    uv run --project server python scripts/scale_run.py --name NAME --day 2026-11-24

`cli` is the command. `datasf` is what a run is scored against, `lists` the streets,
blocks, and units it reads names from, `generate` the calls of a run that has no
recordings, `run` the run's directory, what a run is, and the hearing, `score` the
matching of incidents to DataSF's calls, `taps` the replayed taps, and `results`
the numbers, each with the sentence that says what it is.

A run is one of three kinds for good (`run.Settings`), and its results say which
before they say anything else (`results.basis`): generated calls heard by a stand-in,
generated speech heard by Muse Ember, or recorded radio heard by Muse Ember. Only
the last is about Beacon on real radio.
"""
