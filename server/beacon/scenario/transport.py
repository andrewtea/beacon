"""The mock scenario as an HTTP transport.

In mock mode the pollers are the real ones, and this stands where the network would
be: it answers a request to a source's address with what the scenario says that
source would have answered at the clock's moment. It is strict about what it is
asked, so a poller that would be refused by the real source is refused here: NWS
needs a User-Agent, 511 a token, and OpenSky its bounding box.

A request to any other address gets a 404, and nothing here opens a connection.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx2

from beacon.scenario.fire import Scenario

_OPENSKY_BOX = ("lamin", "lomin", "lamax", "lomax")


def scenario_transport(scenario: Scenario, now: Callable[[], datetime]) -> httpx2.MockTransport:
    """A transport that answers as the scenario's sources would at the moment `now()`."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        url = request.url
        place = (request.method, url.host, url.path)
        if place == ("GET", "api.weather.gov", "/alerts/active"):
            if not request.headers.get("user-agent", "").startswith("Beacon"):
                return httpx2.Response(403, json={"title": "Forbidden"})
            return _json(scenario.nws_alerts(now()), "application/geo+json")
        if place == ("GET", "opensky-network.org", "/api/states/all"):
            if any(name not in url.params for name in _OPENSKY_BOX):
                return httpx2.Response(400, text="A bounding box is required.")
            return _json(scenario.opensky_states(now()))
        if place == ("GET", "api.511.org", "/traffic/events"):
            if not url.params.get("api_key"):
                return httpx2.Response(401, text="Unauthorized")
            if url.params.get("in_effect_on") != "now":
                # Without it 511 also lists work that is planned and has not begun.
                return httpx2.Response(400, text="The mock scenario answers for now only.")
            # 511 begins its replies with a byte order mark.
            return _json(scenario.traffic_events(now()), bom=True)
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
