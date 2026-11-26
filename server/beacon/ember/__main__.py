"""`python -m beacon.ember`: write the token trace from the ledger and print the spend.

Makes no API calls and needs no key.
"""

import os

from beacon.ember.client import EmberClient, Storage, default_data_dir
from beacon.ember.errors import EmberConfigError
from beacon.ember.pricing import SPENT_BEFORE_VARIABLE, read_stop_settings, to_usd


def main() -> None:
    try:
        ledger, spent_before = read_stop_settings(os.environ)
        ember = EmberClient.replay_only(Storage.under(default_data_dir(os.environ)))
    except EmberConfigError as error:
        raise SystemExit(str(error)) from error
    path, calls = ember.export_trace()
    before = (
        f"{SPENT_BEFORE_VARIABLE} is not set, so no paid call can be made"
        if spent_before is None
        else f"on top of ${to_usd(spent_before):.2f} spent before it began"
    )
    print(
        f"Trace: {path}\nCalls: {calls}\nLive spend: ${ember.spent_usd():.6f}\n"
        f"Stop: ${to_usd(ledger):.2f} in this ledger, {before}"
    )


if __name__ == "__main__":
    main()
