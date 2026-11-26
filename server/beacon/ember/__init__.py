"""Muse Ember client: the only code path allowed to call the Meta Model API.

    from beacon.ember import EmberClient

    ember = EmberClient.from_env()
    answer = ember.respond(
        task="extract_transmission",
        effort="low",
        max_output_tokens=1024,
        sensitive=True,
        input="...",
    )

See `client` for what the client does around each call, and `fake` for running with no key.
"""

from beacon.ember.client import (
    Effort,
    EmberClient,
    EmberResponse,
    Responder,
    SpendScope,
    Storage,
    read_scope_spend,
)
from beacon.ember.errors import (
    BudgetExceededError,
    EmberCallError,
    EmberConfigError,
    EmberError,
    EmberRequestError,
    InputTooLargeError,
    LedgerReplacedError,
    RateLimitHoldError,
    ReplayMissError,
)
from beacon.ember.fake import Script, fake_response
from beacon.ember.ledger import LedgerCall, ScopeSpend
from beacon.ember.pacing import Pacer
from beacon.ember.pricing import SpendingStop, Usage

__all__ = [
    "BudgetExceededError",
    "Effort",
    "EmberCallError",
    "EmberClient",
    "EmberConfigError",
    "EmberError",
    "EmberRequestError",
    "EmberResponse",
    "InputTooLargeError",
    "LedgerCall",
    "LedgerReplacedError",
    "Pacer",
    "RateLimitHoldError",
    "ReplayMissError",
    "Responder",
    "ScopeSpend",
    "Script",
    "SpendScope",
    "SpendingStop",
    "Storage",
    "Usage",
    "fake_response",
    "read_scope_spend",
]
