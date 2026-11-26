"""`python -m beacon.ember`: write the token trace from the ledger and print the spend.

Makes no API calls and needs no key.
"""

import os

from beacon.ember.client import EmberClient, Storage, default_data_dir
from beacon.ember.pricing import SpendingStop, to_usd


def main() -> None:
    stop = SpendingStop.from_env(os.environ)
    ember = EmberClient.replay_only(Storage.under(default_data_dir(os.environ)))
    path, calls = ember.export_trace()
    print(
        f"Trace: {path}\nCalls: {calls}\nLive spend: ${ember.spent_usd():.6f}\n"
        f"Stop: ${to_usd(stop.ledger_nanousd):.2f} in this ledger, on top of "
        f"${to_usd(stop.spent_before_nanousd):.2f} spent before it began"
    )


if __name__ == "__main__":
    main()
