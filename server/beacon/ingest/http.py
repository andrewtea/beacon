"""How a poller asks a source for something over HTTP.

Every request a poller makes goes through `fetch`, which caps what it reads and turns
any failure into a `SourceError` with a fixed text. The text never repeats the
address that was asked, because 511's carries the API key, or the reply, which can be
anything.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx2

TIMEOUT_SECONDS = 20.0


class _NoRequestLines(logging.Filter):
    """The HTTP library logs every request's whole address at INFO, and 511's address
    holds the API token. Those lines are dropped whatever level the process logs at:
    a filter on the logger is not undone by a handler or a level set elsewhere."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


logging.getLogger(httpx2.__name__).addFilter(_NoRequestLines())

# The header OpenSky sends beside 429, and the standard one.
_RETRY_HEADERS = ("retry-after", "x-rate-limit-retry-after-seconds")


class SourceError(Exception):
    """A source could not be read. `status` is the HTTP status when it answered with
    an error, and `retry_after` the seconds it asked the caller to wait, when it did."""

    def __init__(
        self,
        source: str,
        problem: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{source} {problem}.")
        self.source = source
        self.status = status
        self.retry_after = retry_after


def http_client(transport: httpx2.BaseTransport | None = None) -> httpx2.Client:
    """The HTTP client the pollers share. It follows no redirect: a source that
    answers with one is reported, not followed to wherever it points. `transport`
    replaces the network, which is how the mock scenario and the tests answer."""
    return httpx2.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport)


def fetch(
    http: httpx2.Client,
    method: str,
    url: str,
    *,
    source: str,
    max_bytes: int,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    data: Mapping[str, str] | None = None,
) -> bytes:
    """The body of a 2xx reply, of at most `max_bytes`.

    Raises SourceError when the source cannot be reached, answers with anything but
    2xx, or sends more than `max_bytes`."""
    try:
        with http.stream(method, url, params=params, headers=headers, data=data) as response:
            if not 200 <= response.status_code < 300:
                raise SourceError(
                    source,
                    f"answered HTTP {response.status_code}",
                    status=response.status_code,
                    retry_after=_retry_after(response.headers),
                )
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise SourceError(source, "sent more than Beacon reads from it")
    except (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError):
        # Nothing is chained under it: the library's error names the address asked.
        raise SourceError(source, "could not be reached") from None
    return bytes(body)


def read_json(body: bytes, source: str) -> Any:
    """The JSON a reply holds. 511 begins its replies with a byte order mark.

    Raises SourceError when the reply is not JSON."""
    try:
        return json.loads(body.decode("utf-8-sig"))
    except ValueError:
        raise SourceError(source, "sent a reply that is not JSON") from None


def _retry_after(headers: httpx2.Headers) -> float | None:
    for name in _RETRY_HEADERS:
        try:
            seconds = float(headers.get(name, ""))
        except ValueError:
            continue
        if 0 <= seconds < float("inf"):
            return seconds
    return None
