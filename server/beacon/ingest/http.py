"""How a poller asks a source for something over HTTP.

Every request a poller makes goes through `fetch`, which caps what it reads and how
long it takes, and turns any failure into a `SourceError` with a fixed text. Neither
the text nor anything chained under the error repeats the address that was asked,
because 511's carries the API key, or the reply, which can be anything.

The client a poller is given, a `SourceHttp`, also says where its rows come from:
`live` when it asks the network and `mock` when a transport answers in the network's
place. That is decided where the client is built and nowhere else, so what a stand-in
answered cannot be stored as a real source's.

An exchange runs on an event loop of its own for as long as it lasts. That is what
lets it be cancelled from outside wherever it is waiting: by its deadline, which
covers the lookup, the connection, the headers, and the body together, and by
`SourceHttp.close` when Beacon stops.

One thing cannot be called off: the lookup of a source's name, which the system does
on a thread and gives no way to stop. A cancelled exchange stops waiting for it, and
the lookup itself goes on until the system's resolver gives up. A client has
`_LOOKUP_THREADS` threads for lookups and no more, so however many exchanges time
out on a resolver that does not answer, that many threads are waiting on it at most,
and Python waits for them, for as long as the resolver takes, when the process ends.
"""

import asyncio
import contextlib
import json
import logging
import re
import ssl
import threading
import zlib
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2

from beacon.errors import raise_bare
from beacon.evidence import Origin

# For each connect, read, and write. `MAX_SECONDS` bounds a whole exchange, from the
# lookup of the source's name to the last byte of its reply.
TIMEOUT_SECONDS = 20.0
MAX_SECONDS = 60.0

# The threads a client has for looking names up. A lookup that hangs keeps one until
# the resolver gives up, and the exchanges that follow share what is left.
_LOOKUP_THREADS = 2

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
    an error, and `retry_after` the seconds it asked the caller to wait, when it did.
    `too_large` says that it answered, with more than the caller reads from it: the
    one failure that asking again does not mend."""

    def __init__(
        self,
        source: str,
        problem: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        too_large: bool = False,
    ) -> None:
        super().__init__(f"{source} {problem}.")
        self.source = source
        self.status = status
        self.retry_after = retry_after
        self.too_large = too_large


_PACKED_ANOTHER_WAY = "packed its reply in a way Beacon does not read"

# `Sun Nov  6 08:49:37 1994`: a day with one digit has a space in front of it.
_ASCTIME = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?:\d{2}| \d) \d{2}:\d{2}:\d{2} \d{4}"
)


class _AsFromTheNetwork(httpx2.AsyncBaseTransport):
    """Wraps a transport that stands in for the network. A reply such a transport
    builds from bytes or JSON is already read when it is made, and `fetch` reads a
    reply as it arrives, so the reply is made into an unread one with the same
    status, headers, and body. A reply the stand-in built as a stream is passed on
    as it is."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
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


class _Loop(asyncio.SelectorEventLoop):
    """An event loop that looks names up on the client's few threads. A loop of the
    usual kind has threads of its own for that, and with a loop for each exchange,
    every lookup that hung would leave a thread behind."""

    def __init__(self, lookups: Executor) -> None:
        super().__init__()
        self._lookups = lookups

    def run_in_executor(
        self, executor: Executor | None, func: Callable[..., Any], *args: Any
    ) -> "asyncio.Future[Any]":
        return super().run_in_executor(self._lookups if executor is None else executor, func, *args)


class _Gunzip:
    """Unpacks a gzip reply a piece at a time, and knows whether it ended where a
    gzip stream ends. A reply can be several gzip members one after another, and
    all of them are the reply."""

    def __init__(self) -> None:
        self._member = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def feed(self, data: bytes, room: int) -> bytes:
        """What `data` unpacks to, up to `room` bytes, which is one or more. The
        caller stops feeding when it is handed that many.

        Raises zlib.error when the data is not gzip."""
        unpacked = bytearray()
        while data:
            if self._member.eof:
                # What follows the end of one member is the beginning of the next.
                self._member = zlib.decompressobj(16 + zlib.MAX_WBITS)
            unpacked += self._member.decompress(data, room - len(unpacked))
            if len(unpacked) >= room:
                break
            # Under the limit everything was read, but for what lies past the end.
            data = self._member.unused_data
        return bytes(unpacked)

    @property
    def complete(self) -> bool:
        """Whether the last member was read to its end, checksum and all."""
        return self._member.eof


class SourceHttp:
    """The pollers' way to the network, or to what stands in for it.

    With no `transport` the requests go to the network, and `origin` is `live`. With
    one, it answers every request and nothing is sent anywhere, which is how the mock
    scenario and the tests answer, and `origin` is `mock`. A poller labels its rows
    with the origin of the client it was given: there is no other way to set it.

    It follows no redirect: a source that answers with one is reported, not followed
    to wherever it points. It is safe to share between threads."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = None if transport is None else _AsFromTheNetwork(transport)
        self._verify: ssl.SSLContext | None = None
        self._lock = threading.Lock()
        self._closed = False
        # The exchanges that are out, each with the loop it runs on.
        self._out: set[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = set()
        # Its threads are begun when a name is first looked up, so a client that is
        # answered by a stand-in never has any.
        self._lookups = ThreadPoolExecutor(_LOOKUP_THREADS, thread_name_prefix="beacon-lookup")

    @property
    def origin(self) -> Origin:
        return "live" if self._transport is None else "mock"

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """End every exchange that is out, wherever it is waiting, and refuse any
        later one. The poller that was waiting gets a SourceError."""
        with self._lock:
            self._closed = True
            out = list(self._out)
        for loop, task in out:
            # An exchange that ended meanwhile has closed its loop, which is fine.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        # A lookup that is out cannot be called off. Its thread ends when it returns.
        self._lookups.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "SourceHttp":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _run[T](self, exchange: Coroutine[Any, Any, T]) -> T | None:
        """Run an exchange to its end on a loop of its own, and return what it
        returned, or None when `close` cancelled it."""
        loop = _Loop(self._lookups)
        try:
            task = loop.create_task(exchange)
            with self._lock:
                if self._closed:
                    task.cancel()
                self._out.add((loop, task))
            try:
                return loop.run_until_complete(task)
            except asyncio.CancelledError:
                return None
            except RuntimeError:
                # `close` shut the lookup threads down just as the exchange went to
                # look a name up, before the cancellation reached it.
                if self._closed:
                    return None
                raise
            finally:
                with self._lock:
                    self._out.discard((loop, task))
                # An interrupt from the keyboard leaves the exchange unfinished.
                unfinished = asyncio.all_tasks(loop)
                for left in unfinished:
                    left.cancel()
                if unfinished:
                    loop.run_until_complete(asyncio.gather(*unfinished, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            # Closing the loop does not wait for a name lookup that is still out on
            # one of the client's threads, as `asyncio.run` would for minutes.
            loop.close()

    async def _exchange(
        self,
        method: str,
        url: str,
        *,
        source: str,
        max_bytes: int,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
    ) -> "bytes | SourceError":
        """The body of a 2xx reply, or the error to raise. It is returned and not
        raised here, so that it is raised with nothing under it."""
        body = bytearray()
        try:
            async with (
                asyncio.timeout(MAX_SECONDS),
                httpx2.AsyncClient(
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=False,
                    transport=self._transport,
                    verify=self._certificates(),
                ) as client,
                client.stream(
                    method,
                    url,
                    params=params,
                    # Asked for explicitly, so that the reply is plain or gzip and
                    # nothing else. It is unpacked here, a piece at a time, and not
                    # by the library, which would unpack a small reply into any
                    # amount of memory.
                    headers={**(headers or {}), "Accept-Encoding": "gzip"},
                    data=data,
                ) as response,
            ):
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if not 200 <= response.status_code < 300:
                    return SourceError(
                        source,
                        f"answered HTTP {response.status_code}",
                        status=response.status_code,
                        retry_after=_retry_after(response.headers),
                    )
                if encoding not in ("", "identity", "gzip"):
                    return SourceError(source, _PACKED_ANOTHER_WAY)
                unpack = _Gunzip() if encoding == "gzip" else None
                async for chunk in response.aiter_raw():
                    # One byte past the cap is enough to know the reply is over it.
                    room = max_bytes + 1 - len(body)
                    body += chunk if unpack is None else unpack.feed(chunk, room)
                    if len(body) > max_bytes:
                        return SourceError(
                            source, "sent more than Beacon reads from it", too_large=True
                        )
                if unpack is not None and not unpack.complete:
                    # Cut short, or not gzip to its end: part of a reply is no reply.
                    return SourceError(source, _PACKED_ANOTHER_WAY)
        except TimeoutError:
            return SourceError(source, "took too long to answer")
        except zlib.error:
            return SourceError(source, _PACKED_ANOTHER_WAY)
        except (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError):
            # The library's error names the address that was asked.
            return SourceError(source, "could not be reached")
        return bytes(body)

    def _certificates(self) -> ssl.SSLContext | bool:
        """The certificates to check a source against, read once: every exchange has
        a client of its own, and reading them is most of what building one costs."""
        if self._transport is not None:
            return True
        with self._lock:
            if self._verify is None:
                self._verify = httpx2.create_ssl_context()
            return self._verify


def fetch(
    http: SourceHttp,
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
    2xx, sends more than `max_bytes`, packs its reply in a way Beacon does not read
    or does not finish it, takes longer than `MAX_SECONDS` over the whole exchange,
    or when the client is closed, before or during the exchange."""
    outcome = http._run(
        http._exchange(
            method,
            url,
            source=source,
            max_bytes=max_bytes,
            params=params,
            headers=headers,
            data=data,
        )
    )
    if outcome is None:
        raise_bare(SourceError(source, "was not read, because Beacon is stopping"))
    if isinstance(outcome, SourceError):
        raise_bare(outcome)
    return outcome


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
    """The seconds a reply asks the caller to wait, counted from the reply. The
    standard header says it as a number of seconds or as a date, and OpenSky's own
    as a number."""
    for name in _RETRY_HEADERS:
        value = headers.get(name, "").strip()
        try:
            seconds: float | None = float(value)
        except ValueError:
            seconds = _seconds_until(value, headers.get("date")) if name == "retry-after" else None
        if seconds is not None and 0 <= seconds < float("inf"):
            return seconds
    return None


def _seconds_until(when: str, sent: str | None) -> float | None:
    """The seconds from when a reply was sent to the HTTP date `when`, or None when
    it is not one. A reply says when it was sent in its `Date` header, which is the
    source's clock, as `when` is. Without one, this machine's clock is used. A date
    that has passed is no wait."""
    until = _http_date(when)
    if until is None:
        return None
    since = _http_date(sent or "") or datetime.now(UTC)
    return max(0.0, (until - since).total_seconds())


def _http_date(value: str) -> datetime | None:
    """An HTTP date in any of the three forms RFC 9110 allows, or None."""
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.utcoffset() is not None:
        return moment
    # The oldest form, C's asctime, names no zone and is in GMT like the others. Any
    # other date with no zone is not an HTTP date.
    return moment.replace(tzinfo=UTC) if _ASCTIME.fullmatch(value.strip()) else None
