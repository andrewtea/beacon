"""Errors the Ember client raises. Callers outside `beacon.ember` catch these, not SDK errors."""


class EmberError(Exception):
    """Base class for every error the Ember client raises."""


class EmberConfigError(EmberError):
    """The environment asks for something the client cannot do, such as a live call with no key."""


class EmberRequestError(EmberError, ValueError):
    """The request breaks a client rule, such as a hosted tool or a missing output limit."""


class BudgetExceededError(EmberError):
    """The call was refused because it could take the ledger past the spending stop."""


class LedgerReplacedError(BudgetExceededError):
    """A paid call was refused because the ledger file is not the one this client
    opened: what has been spent is not known from it, so the stop cannot be kept."""


class RateLimitHoldError(EmberError):
    """The API asked for a wait longer than a paced client waits. Nothing is sent
    through that client's pacer until the wait has passed."""


class InputTooLargeError(EmberError):
    """The call was refused because its input is over the per-request token limit."""


class ReplayMissError(EmberError):
    """Replay-only mode has no recorded response for this request."""


class EmberCallError(EmberError):
    """The Meta Model API rejected the call, or it ended without a usable response.

    `status_code` is the HTTP status when the API answered with an error, else None.
    `retry_after` is the seconds its `Retry-After` header asked for, when it sent one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
