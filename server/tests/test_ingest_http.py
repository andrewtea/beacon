"""How a poller's request fails: with a fixed sentence, and nothing of what was asked."""

import logging

import httpx2
import pytest

from beacon.ingest.http import SourceError, fetch, http_client, read_json

URL = "https://api.511.org/traffic/events"
KEY = {"api_key": "secret-token-123"}


def client(handler) -> httpx2.Client:
    return http_client(httpx2.MockTransport(handler))


def get(handler, *, max_bytes: int = 1000) -> bytes:
    return fetch(client(handler), "GET", URL, source="511", max_bytes=max_bytes, params=KEY)


def test_a_reply_is_returned_whole_up_to_the_cap() -> None:
    assert get(lambda request: httpx2.Response(200, content=b"x" * 1000)) == b"x" * 1000

    with pytest.raises(SourceError, match=r"^511 sent more than Beacon reads from it\.$"):
        get(lambda request: httpx2.Response(200, content=b"x" * 1001))


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [
        ({}, None),
        ({"Retry-After": "7"}, 7.0),
        ({"X-Rate-Limit-Retry-After-Seconds": "412"}, 412.0),
        ({"Retry-After": "soon", "X-Rate-Limit-Retry-After-Seconds": "30"}, 30.0),
        ({"Retry-After": "-5"}, None),
        ({"Retry-After": "inf"}, None),
    ],
)
def test_an_error_status_is_reported_with_the_wait_the_source_asked_for(
    headers: dict[str, str], retry_after: float | None
) -> None:
    def refuses(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers=headers, text=f"Too many from {request.url}")

    with pytest.raises(SourceError) as failure:
        get(refuses)

    assert str(failure.value) == "511 answered HTTP 429."
    assert (failure.value.source, failure.value.status) == ("511", 429)
    assert failure.value.retry_after == retry_after


def test_a_redirect_is_reported_and_not_followed() -> None:
    asked: list[str] = []

    def redirects(request: httpx2.Request) -> httpx2.Response:
        asked.append(request.url.host)
        return httpx2.Response(302, headers={"Location": "https://elsewhere.example/"})

    with pytest.raises(SourceError, match="answered HTTP 302"):
        get(redirects)
    assert asked == ["api.511.org"]


def test_a_failure_to_connect_does_not_repeat_the_address_with_its_key() -> None:
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"Could not reach {request.url}", request=request)

    with pytest.raises(SourceError) as failure:
        get(unreachable)

    assert str(failure.value) == "511 could not be reached."
    # The library's own error names the address, so it is not kept under this one.
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__
    assert failure.value.status is None


def test_no_log_line_holds_the_address_of_a_request(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    get(lambda request: httpx2.Response(200, content=b"{}"))
    with pytest.raises(SourceError):
        get(lambda request: httpx2.Response(401))

    # The HTTP library logs each request's address at INFO unless it is stopped.
    assert "secret-token-123" not in caplog.text
    assert "api_key" not in caplog.text


def test_json_is_read_past_a_byte_order_mark() -> None:
    assert read_json(b'\xef\xbb\xbf{"events": []}', "511") == {"events": []}
    assert read_json(b"[1, 2]", "511") == [1, 2]


@pytest.mark.parametrize("body", [b"<html>Jane Doe</html>", b"\xff\xfe", b""])
def test_a_reply_that_is_not_json_is_reported_without_repeating_it(body: bytes) -> None:
    with pytest.raises(SourceError) as failure:
        read_json(body, "nws")

    assert str(failure.value) == "nws sent a reply that is not JSON."
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__
