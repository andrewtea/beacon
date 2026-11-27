"""The mock scenario as an HTTP transport.

In mock mode the pollers are the real ones, and this stands where the network would
be: it answers a request to a source's address with what the scenario says that
source would have answered at the clock's moment. It is strict about what it is
asked, so a poller that would be refused by the real source is refused here: NWS
needs a User-Agent, 511 a token, adsb.lol a point and a radius it answers for, and
OpenMHz a time and the talkgroups to list. The notices feed is on a host of the story's own.

A request to any other address gets a 404, and nothing here opens a connection.
"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx2

from beacon.scenario import notices, radio
from beacon.scenario.fire import Scenario

# `/v2/point/{lat}/{lon}/{radius}`, with the radius in whole nautical miles.
_ADSBLOL_POINT = re.compile(r"/v2/point/(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)/(\d+)")


def scenario_transport(scenario: Scenario, now: Callable[[], datetime]) -> httpx2.MockTransport:
    """A transport that answers as the scenario's sources would at the moment `now()`."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        url = request.url
        place = (request.method, url.host, url.path)
        if place == ("GET", "api.weather.gov", "/alerts/active"):
            if not request.headers.get("user-agent", "").startswith("Beacon"):
                return httpx2.Response(403, json={"title": "Forbidden"})
            return _json(scenario.nws_alerts(now()), "application/geo+json")
        if (request.method, url.host) == ("GET", "api.adsb.lol"):
            asked = _ADSBLOL_POINT.fullmatch(url.path)
            if asked is None:
                return httpx2.Response(404, json={"detail": "Not Found"})
            if not (
                abs(float(asked[1])) <= 90 and abs(float(asked[2])) <= 180 and int(asked[3]) <= 250
            ):
                return httpx2.Response(422, json={"detail": "A point and up to 250 nm."})
            return _json(scenario.adsblol_point(now()))
        if place == ("GET", "api.511.org", "/traffic/events"):
            if not url.params.get("api_key"):
                return httpx2.Response(401, text="Unauthorized")
            if url.params.get("in_effect_on") != "now":
                # Without it 511 also lists work that is planned and has not begun.
                return httpx2.Response(400, text="The mock scenario answers for now only.")
            # 511 begins its replies with a byte order mark.
            return _json(scenario.traffic_events(now()), bom=True)
        if place == ("GET", "api.openmhz.com", f"/{radio.SYSTEM}/calls/newer"):
            after, codes = url.params.get("time", ""), url.params.get("filter-code", "")
            if not after.isdigit() or url.params.get("filter-type") != "talkgroup":
                return httpx2.Response(400, text="A time and talkgroups are required.")
            talkgroups = {int(code) for code in codes.split(",") if code.isdigit()}
            return _json(radio.calls_answer(scenario.start, now(), int(after), talkgroups))
        if (request.method, url.host) == ("GET", radio.AUDIO_HOST):
            audio = radio.call_audio(url.path.removeprefix("/mock/"))
            if audio is None:
                return httpx2.Response(404, text="The mock scenario has no such call.")
            return httpx2.Response(200, content=audio, headers={"content-type": "audio/wav"})
        if place == ("GET", notices.FEED_HOST, "/feed.json"):
            return _json(notices.feed(scenario.start, now()), "application/feed+json")
        if (request.method, url.host) == ("GET", notices.FEED_HOST):
            document = notices.document(url.path.removeprefix("/documents/"))
            if document is None:
                return httpx2.Response(404, text="The mock scenario has no such document.")
            return httpx2.Response(
                200, content=document, headers={"content-type": "application/pdf"}
            )
        return httpx2.Response(404, text="The mock scenario has no such source.")

    return httpx2.MockTransport(answer)


def _json(
    payload: dict[str, Any], content_type: str = "application/json", *, bom: bool = False
) -> httpx2.Response:
    body = json.dumps(payload).encode()
    return httpx2.Response(
        200,
        content=(b"\xef\xbb\xbf" if bom else b"") + body,
        headers={"content-type": content_type},
    )
