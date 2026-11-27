"""Run a day of dispatch calls through Beacon and score it against DataSF's record.

    uv run --project server python scripts/scale_run.py --name NAME --day YYYY-MM-DD

With no more than that, a stand-in hears generated calls: no key, no cost, and
results that say so. See `beacon.scale.cli` for a paid run, on generated speech or
on recordings. The same command, given again, goes on with a run that stopped.
"""

import sys

from beacon.scale.cli import main

if __name__ == "__main__":
    sys.exit(main())
