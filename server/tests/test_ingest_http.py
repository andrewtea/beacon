"""How a poller's request fails: with a fixed sentence, and nothing of what was asked.

The exchanges here are real ones with a server on this machine, so the HTTP library
and the transport under it do everything they do on the network, logging included.
"""

import contextlib
import gzip
import logging
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest

from beacon.ingest import http as source_http
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.nws import parse_alerts
from beacon.ingest.opensky import parse_states
from beacon.ingest.traffic511 import parse_events

TOKEN = "secret-token-123"

Reply = Callable[[BaseHTTPRequestHandler], None]


class Server:
    """A loopback HTTP server that answers every request with `reply`."""

    def __init__(self) -> None:
        self.reply: Reply = lambda handler: send(handler, 200, b"{}")
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.paths.append(self.path)
                outer.headers.append({name.lower(): value for name, value in self.headers.items()})
                outer.reply(self)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/traffic/events"
        threading.Thread(target=self._server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def send(handler: BaseHTTPRequestHandler, status: int, body: bytes = b"", **headers: str) -> None:
    if "Date" in headers:
        # Without the date the server would put in front of the one given.
        handler.send_response_only(status)
    else:
        handler.send_response(status)
    for name, value in headers.items():
        handler.send_header(name.replace("_", "-"), value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture
def server() -> Iterator[Server]:
    server = Server()
    yield server
    server.close()


def get(server: Server, *, max_bytes: int = 1000) -> bytes:
    with SourceHttp() as http:
        return fetch(
            http, "GET", server.url, source="511", max_bytes=max_bytes, params={"api_key": TOKEN}
        )


def test_a_reply_is_returned_whole_up_to_the_cap(server: Server) -> None:
    server.reply = lambda handler: send(handler, 200, b"x" * 1000)
    assert get(server) == b"x" * 1000

    server.reply = lambda handler: send(handler, 200, b"x" * 1001)
    with pytest.raises(SourceError, match=r"^511 sent more than Beacon reads from it\.$"):
        get(server)


def test_a_gzipped_reply_is_unpacked_no_further_than_the_cap(server: Server) -> None:
    packed = gzip.compress(b"x" * 1000)
    server.reply = lambda handler: send(handler, 200, packed, Content_Encoding="gzip")
    assert get(server) == b"x" * 1000
    # Beacon asks for gzip or nothing, whatever the library would have offered.
    assert server.headers[-1]["accept-encoding"] == "gzip"

    # A hundred megabytes of zeros is a hundred kilobytes packed. It is given up on
    # at the cap, and the rest is never unpacked.
    bomb = gzip.compress(bytes(100_000_000))
    assert len(bomb) < 200_000
    server.reply = lambda handler: send(handler, 200, bomb, Content_Encoding="gzip")
    tracemalloc.start()
    try:
        with pytest.raises(SourceError, match="sent more than Beacon reads"):
            get(server)
        _now, most = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert most < 5_000_000

    server.reply = lambda handler: send(handler, 200, b"not gzip", Content_Encoding="gzip")
    with pytest.raises(SourceError, match="packed its reply in a way Beacon does not read"):
        get(server)


def test_a_gzipped_reply_is_read_to_the_end_of_every_part_or_not_at_all(server: Server) -> None:
    first, second = gzip.compress(b'{"events": ['), gzip.compress(b"1, 2]}")

    # Gzip allows a reply in several parts, one after another, and all are the reply.
    server.reply = lambda handler: send(handler, 200, first + second, Content_Encoding="gzip")
    assert get(server) == b'{"events": [1, 2]}'
    # The cap is on all of them together.
    with pytest.raises(SourceError, match="sent more than Beacon reads"):
        get(server, max_bytes=17)
    assert get(server, max_bytes=18) == b'{"events": [1, 2]}'

    whole = gzip.compress(b'{"events": []}')
    for cut_short in (
        # Without its checksum and length, with half of them, and with a second part
        # that only begins.
        whole[:-8],
        whole[:-4],
        whole + second[:5],
        # Something after the end that is not gzip.
        whole + b"{}",
        b"",
    ):
        server.reply = lambda handler, body=cut_short: send(
            handler, 200, body, Content_Encoding="gzip"
        )
        with pytest.raises(SourceError, match="packed its reply in a way Beacon does not read"):
            get(server)


def test_a_reply_packed_another_way_is_refused(server: Server) -> None:
    server.reply = lambda handler: send(handler, 200, b"\x00\x01", Content_Encoding="br")

    with pytest.raises(SourceError, match="packed its reply in a way Beacon does not read"):
        get(server)


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [
        ({}, None),
        ({"Retry_After": "7"}, 7.0),
        ({"X_Rate_Limit_Retry_After_Seconds": "412"}, 412.0),
        ({"Retry_After": "soon", "X_Rate_Limit_Retry_After_Seconds": "30"}, 30.0),
        ({"Retry_After": "-5"}, None),
        # A date, counted from when the source says it answered and not from this
        # machine's clock, which may be set differently.
        (
            {
                "Retry_After": "Fri, 27 Nov 2099 20:30:00 GMT",
                "Date": "Fri, 27 Nov 2099 20:00:00 GMT",
            },
            1800.0,
        ),
        (
            {
                "Retry_After": "Fri, 27 Nov 2099 19:00:00 GMT",
                "Date": "Fri, 27 Nov 2099 20:00:00 GMT",
            },
            0.0,
        ),
        # A date that has passed by this machine's clock, with no date to count from.
        ({"Retry_After": "Fri, 27 Nov 2020 20:30:00 GMT"}, 0.0),
        ({"Retry_After": "27 Nov 2099 20:30:00"}, None),
        # The two older forms of an HTTP date. The oldest names no zone and is GMT.
        (
            {"Retry_After": "Sun Nov  6 09:49:37 1994", "Date": "Sun, 06 Nov 1994 08:49:37 GMT"},
            3600.0,
        ),
        (
            {"Retry_After": "Sunday, 06-Nov-94 08:59:37 GMT", "Date": "Sun Nov  6 08:49:37 1994"},
            600.0,
        ),
        ({"Retry_After": "Sun Nov 6 09:49:37 1994", "Date": "Sun, 06 Nov 1994 08:49:37 GMT"}, None),
        # OpenSky's own header is a number and nothing else.
        ({"X_Rate_Limit_Retry_After_Seconds": "Fri, 27 Nov 2099 20:30:00 GMT"}, None),
        ({"Retry_After": "inf"}, None),
        ({"Retry_After": "nan"}, None),
    ],
)
def test_an_error_status_is_reported_with_the_wait_the_source_asked_for(
    server: Server, headers: dict[str, str], retry_after: float | None
) -> None:
    server.reply = lambda handler: send(
        handler, 429, f"Too many from {handler.path}".encode(), **headers
    )

    with pytest.raises(SourceError) as failure:
        get(server)

    assert str(failure.value) == "511 answered HTTP 429."
    assert (failure.value.source, failure.value.status) == ("511", 429)
    assert failure.value.retry_after == retry_after


def test_a_redirect_is_reported_and_not_followed(server: Server) -> None:
    server.reply = lambda handler: send(handler, 302, Location=server.url + "/elsewhere")

    with pytest.raises(SourceError, match="answered HTTP 302"):
        get(server)

    assert server.paths == [f"/traffic/events?api_key={TOKEN}"]


def _everything_under(error: BaseException) -> list[BaseException]:
    found = []
    while error is not None:
        found.append(error)
        error = error.__cause__ or error.__context__
    return found


def test_a_failure_to_connect_keeps_nothing_of_the_address_with_its_key(server: Server) -> None:
    server.close()

    with pytest.raises(SourceError) as failure:
        get(server)

    assert str(failure.value) == "511 could not be reached."
    # The library's own error holds the request, so it is not kept under this one,
    # as a cause or as the error that was being handled.
    assert _everything_under(failure.value) == [failure.value]
    assert failure.value.status is None


def _while_handling(error: Exception, call: Callable[[], object]) -> None:
    """Make a call the way a caller does who is itself handling an error."""
    try:
        raise error
    except type(error):
        call()


def test_a_failure_keeps_nothing_of_an_error_the_caller_was_handling(server: Server) -> None:
    server.reply = lambda handler: send(handler, 500)

    with pytest.raises(SourceError) as failure:
        _while_handling(ValueError(f"api_key={TOKEN}"), lambda: get(server))
    assert _everything_under(failure.value) == [failure.value]

    for reading in (
        lambda: read_json(b"not json", "nws"),
        # What reads the JSON raises the same way.
        lambda: parse_alerts({"title": "Jane Doe"}, "mock"),
        lambda: parse_events(["Jane Doe"], "mock"),
        lambda: parse_states({"message": "Jane Doe"}, "mock"),
    ):
        with pytest.raises(SourceError) as failure:
            _while_handling(ValueError("Jane Doe"), reading)
        assert _everything_under(failure.value) == [failure.value]


def test_no_log_line_at_any_level_holds_the_address_of_a_request(
    server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    # Every logger at its most talkative, as a developer chasing a bug would set it.
    caplog.set_level(logging.DEBUG)
    for name in ("httpx2", "httpcore2", "httpcore2.http11", "httpcore2.connection"):
        caplog.set_level(logging.DEBUG, logger=name)

    get(server)
    # A redirect's Location header is the address again, token and all.
    server.reply = lambda handler: send(
        handler, 302, Location=f"{server.url}?api_key={TOKEN}", Retry_After="5"
    )
    with pytest.raises(SourceError):
        get(server)
    server.reply = lambda handler: send(handler, 401, f"Bad key {TOKEN}".encode())
    with pytest.raises(SourceError):
        get(server)

    assert len(server.paths) == 3
    assert TOKEN not in caplog.text
    assert "api_key" not in caplog.text


def _drip(handler: BaseHTTPRequestHandler, pieces: list[bytes], apart: float) -> None:
    # Beacon hangs up at its deadline, and the next piece then has nowhere to go.
    with contextlib.suppress(OSError):
        for piece in pieces:
            handler.wfile.write(piece)
            handler.wfile.flush()
            time.sleep(apart)


@pytest.mark.parametrize(
    "reply",
    [
        # Nothing at all, headers a line at a time, and a body a byte at a time. No
        # single wait in the last two is long: it is the whole that is.
        lambda handler: time.sleep(3),
        lambda handler: _drip(
            handler, [b"HTTP/1.1 200 OK\r\n"] + [b"X-Slow: 1\r\n"] * 60, apart=0.05
        ),
        lambda handler: _drip(
            handler, [b"HTTP/1.1 200 OK\r\nContent-Length: 60\r\n\r\n"] + [b"x"] * 60, apart=0.05
        ),
    ],
    ids=["silent", "slow headers", "slow body"],
)
def test_an_exchange_that_takes_too_long_as_a_whole_is_given_up_on(
    server: Server, monkeypatch: pytest.MonkeyPatch, reply: Reply
) -> None:
    monkeypatch.setattr(source_http, "MAX_SECONDS", 0.5)
    server.reply = reply
    began = time.monotonic()

    with pytest.raises(SourceError) as failure:
        get(server)

    assert str(failure.value) == "511 took too long to answer."
    assert _everything_under(failure.value) == [failure.value]
    # At the deadline, and not when the source would have finished, three seconds in.
    assert 0.5 <= time.monotonic() - began < 2.5


def test_lookups_that_hang_do_not_leave_a_thread_behind_for_each_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answered = threading.Event()
    asked: list[str] = []

    def hang(host: str, *args: object, **kwargs: object) -> list:
        # A resolver that does not answer. Nothing can call a lookup off.
        asked.append(host)
        answered.wait(30)
        return []

    monkeypatch.setattr("socket.getaddrinfo", hang)
    monkeypatch.setattr(source_http, "MAX_SECONDS", 0.2)
    http = SourceHttp()

    for _exchange in range(5):
        with pytest.raises(SourceError, match=r"^nws took too long to answer\.$"):
            fetch(http, "GET", "http://source.invalid/alerts", source="nws", max_bytes=10)

    # Five exchanges timed out, and no more lookups are waiting than the client has
    # threads for. The others never began.
    waiting = [
        thread for thread in threading.enumerate() if thread.name.startswith("beacon-lookup")
    ]
    assert len(waiting) == len(asked) == 2
    http.close()
    answered.set()
    for thread in waiting:
        thread.join(5)
    assert not any(thread.is_alive() for thread in waiting)


def test_closing_the_client_ends_an_exchange_that_is_out_and_refuses_the_next(
    server: Server,
) -> None:
    asked = threading.Event()

    def never_answer(handler: BaseHTTPRequestHandler) -> None:
        asked.set()
        time.sleep(3)

    server.reply = never_answer
    http = SourceHttp()
    failures: list[BaseException] = []

    def ask() -> None:
        try:
            fetch(http, "GET", server.url, source="511", max_bytes=1000)
        except SourceError as failure:
            failures.append(failure)

    asking = threading.Thread(target=ask)
    asking.start()
    assert asked.wait(5)
    began = time.monotonic()

    http.close()
    asking.join(2)

    assert not asking.is_alive()
    assert time.monotonic() - began < 1
    assert http.closed
    with pytest.raises(SourceError) as refused:
        fetch(http, "GET", server.url, source="511", max_bytes=1000)
    assert [str(failure) for failure in (*failures, refused.value)] == [
        "511 was not read, because Beacon is stopping."
    ] * 2
    # The second was never sent.
    assert len(server.paths) == 1


def test_a_client_says_whether_it_asks_the_network_or_a_stand_in() -> None:
    assert SourceHttp().origin == "live"
    assert SourceHttp(httpx2.MockTransport(lambda request: httpx2.Response(200))).origin == "mock"


def test_a_stand_in_for_the_network_is_read_the_way_the_network_is() -> None:
    packed = gzip.compress(b'{"events": []}')

    def stand_in(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/packed":
            return httpx2.Response(
                200, stream=httpx2.ByteStream(packed), headers={"Content-Encoding": "gzip"}
            )
        return httpx2.Response(200, json={"events": []}, headers={"Retry-After": "3"})

    http = SourceHttp(httpx2.MockTransport(stand_in))

    for path in ("/plain", "/packed"):
        body = fetch(http, "GET", f"https://api.511.org{path}", source="511", max_bytes=100)
        assert read_json(body, "511") == {"events": []}
    with pytest.raises(SourceError, match="sent more than Beacon reads"):
        fetch(http, "GET", "https://api.511.org/plain", source="511", max_bytes=5)


def test_json_is_read_past_a_byte_order_mark() -> None:
    assert read_json(b'\xef\xbb\xbf{"events": []}', "511") == {"events": []}
    assert read_json(b"[1, 2]", "511") == [1, 2]


@pytest.mark.parametrize("body", [b"<html>Jane Doe</html>", b"\xff\xfe", b""])
def test_a_reply_that_is_not_json_is_reported_without_keeping_it(body: bytes) -> None:
    with pytest.raises(SourceError) as failure:
        read_json(body, "nws")

    assert str(failure.value) == "nws sent a reply that is not JSON."
    # The decoder's error holds the whole reply.
    assert _everything_under(failure.value) == [failure.value]
