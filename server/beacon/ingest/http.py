"""How a poller asks a source for something over HTTP.

Every request a poller makes goes through `fetch`, which caps what it reads and how
long it takes, and turns any failure into a `SourceError` with a fixed text. Neither
the text nor anything chained under the error repeats the address that was asked,
because 511's carries the API key, or the reply, which can be anything.
"""

import json
import logging
import time
import zlib
from collections.abc import Mapping
from typing import Any, NoReturn

import httpx2

# For each connect, read, and write. `MAX_SECONDS` bounds a whole exchange.
TIMEOUT_SECONDS = 20.0
MAX_SECONDS = 60.0

_CHUNK_BYTES = 65_536

# The header OpenSky sends beside 429, and the standard one.
_RETRY_HEADERS = ("retry-after", "x-rate-limit-retry-after-seconds")

# The HTTP library logs every request's whole address at INFO, and the transport
# under it logs every header of every reply at DEBUG, which for a redirect is an
# address again. 511's address holds the API token.
_TRANSPORT_LOGGERS = (
    "httpx2",
    "httpcore2",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
)


class _NoRequestLines(logging.Filter):
    """Drops what the HTTP libraries log below a warning, whatever level the process
    logs at: a filter on a logger is not undone by a handler or a level set elsewhere."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


for _name in _TRANSPORT_LOGGERS:
    logging.getLogger(_name).addFilter(_NoRequestLines())
# A filter covers only its own logger, so the level is set on the two roots as well:
# a logger a later version of either library adds below them inherits it.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)


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


def raise_bare(error: Exception) -> NoReturn:
    """Raise an error with nothing under it. Called outside any `except` block, so
    the error being handled there, which can hold a reply or an address, is not kept
    as this one's context. Python would still put an error the caller is handling
    under it, and that one is not this error's to show either, so it is removed."""
    try:
        raise error from None
    finally:
        error.__context__ = None


class _AsFromTheNetwork(httpx2.BaseTransport):
    """Wraps a transport that stands in for the network. A reply such a transport
    builds from bytes or JSON is already read when it is made, and `fetch` reads a
    reply as it arrives, so the reply is made into an unread one with the same
    status, headers, and body. A reply the stand-in built as a stream is passed on
    as it is."""

    def __init__(self, inner: httpx2.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = self._inner.handle_request(request)
        if not response.is_stream_consumed:
            return response
        # The body is as the stand-in gave it, so it has no packing or length to declare.
        headers = [
            (name, value)
            for name, value in response.headers.raw
            if name.lower() not in (b"content-encoding", b"content-length")
        ]
        return httpx2.Response(
            response.status_code, headers=headers, stream=httpx2.ByteStream(response.content)
        )

    def close(self) -> None:
        self._inner.close()


def http_client(transport: httpx2.BaseTransport | None = None) -> httpx2.Client:
    """The HTTP client the pollers share. It follows no redirect: a source that
    answers with one is reported, not followed to wherever it points. `transport`
    replaces the network, which is how the mock scenario and the tests answer."""
    return httpx2.Client(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=None if transport is None else _AsFromTheNetwork(transport),
    )


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
    """The body of a 2xx reply, of at most `max_bytes` once it is unpacked.

    Raises SourceError when the source cannot be reached, answers with anything but
    2xx, sends more than `max_bytes`, packs its reply in a way Beacon does not read,
    or takes longer than `MAX_SECONDS`."""
    problem: SourceError | None = None
    body = bytearray()
    started = time.monotonic()
    try:
        with http.stream(
            method,
            url,
            params=params,
            # Asked for explicitly, so that the reply is plain or gzip and nothing
            # else. It is unpacked here, a piece at a time, and not by the library,
            # which would unpack a small reply into any amount of memory.
            headers={**(headers or {}), "Accept-Encoding": "gzip"},
            data=data,
        ) as response:
            encoding = response.headers.get("content-encoding", "").strip().lower()
            if not 200 <= response.status_code < 300:
                problem = SourceError(
                    source,
                    f"answered HTTP {response.status_code}",
                    status=response.status_code,
                    retry_after=_retry_after(response.headers),
                )
            elif encoding not in ("", "identity", "gzip"):
                problem = SourceError(source, "packed its reply in a way Beacon does not read")
            else:
                unpack = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
                for chunk in response.iter_raw(_CHUNK_BYTES):
                    if time.monotonic() - started > MAX_SECONDS:
                        problem = SourceError(source, "took too long to answer")
                        break
                    # One byte past the cap is enough to know the reply is over it.
                    room = max_bytes + 1 - len(body)
                    body += chunk if unpack is None else unpack.decompress(chunk, room)
                    if len(body) > max_bytes:
                        problem = SourceError(source, "sent more than Beacon reads from it")
                        break
    except (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError, zlib.error):
        # The library's error names the address that was asked.
        problem = SourceError(source, "could not be reached")
    if problem is not None:
        raise_bare(problem)
    return bytes(body)


def read_json(body: bytes, source: str) -> Any:
    """The JSON a reply holds. 511 begins its replies with a byte order mark.

    Raises SourceError when the reply is not JSON."""
    try:
        return json.loads(body.decode("utf-8-sig"))
    except ValueError:
        # The decoder's error holds the whole reply.
        pass
    raise_bare(SourceError(source, "sent a reply that is not JSON"))


def _retry_after(headers: httpx2.Headers) -> float | None:
    for name in _RETRY_HEADERS:
        try:
            seconds = float(headers.get(name, ""))
        except ValueError:
            continue
        if 0 <= seconds < float("inf"):
            return seconds
    return None
