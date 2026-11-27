"""A server behind a host nobody has checked (`BEACON_FRONT_END=unverified`) takes
nothing of a visitor's own: only the invented scenario's two places."""

import base64
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beacon import front
from beacon.evidence import Point
from beacon.main import ServerSetupError, create_app

SHOWN = {"Beacon-Mode": "mock"}
FIRE = {"lat": 37.776, "lon": -122.424}
ACROSS = {"lat": 37.8, "lon": -122.41}
# Somebody at a window on Hayes Street, about a block from the invented place. The
# position is invented.
OWN = {"lat": 37.7751234, "lon": -122.4198765}
PRECISE = ("37.7751234", "122.4198765", "7751234", "4198765", "37.775", "122.42")


@pytest.fixture
def unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(front.VARIABLE, "unverified")
    # A server that would send notifications, were it not for the host.
    monkeypatch.setenv("BEACON_VAPID_SUBJECT", "mailto:beacon@example.org")


def tap_rows(client: TestClient) -> int:
    return len(client.app.state.taps.store.search(kinds=["tap"], limit=50))  # type: ignore[attr-defined]


def test_the_two_places_are_the_ones_the_app_offers() -> None:
    app_side = (Path(__file__).parents[2] / "web/src/lib/geo.ts").read_text(encoding="utf-8")
    listed = app_side[app_side.index("export const MOCK_SPOTS") :]
    listed = listed[: listed.index("\n]")]
    spots = re.findall(r"place: \{ lat: ([-\d.]+), lon: ([-\d.]+) \}", listed)

    assert tuple(Point(float(lat), float(lon)) for lat, lon in spots) == front.SPOTS
    assert len(front.SPOTS) == 2


def test_the_setting_is_one_of_two_words_and_not_set_is_a_developers_machine() -> None:
    assert front.input_of({}) == "own"
    assert front.input_of({front.VARIABLE: "verified"}) == "own"
    assert front.input_of({front.VARIABLE: "unverified"}) == "invented"
    # Set to nothing is not "not set": a deployment's default is not emptied so.
    for wrong in ("", " ", "probably fine", "Verified"):
        with pytest.raises(front.FrontError, match="is verified or unverified"):
            front.input_of({front.VARIABLE: wrong})


@pytest.mark.usefixtures("unverified")
def test_a_tap_is_taken_only_from_an_invented_place_with_nothing_else() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["input"] == "invented"
        for place in (FIRE, ACROSS):
            taken = client.post("/api/taps", headers=SHOWN, json={"location": place})
            assert taken.status_code == 201
            assert client.get(f"/api/taps/{taken.json()['id']}").status_code == 200
        assert tap_rows(client) == 2

        clip = base64.b64encode(b"RIFF not looked at").decode()
        for body in (
            {"location": OWN},
            # A place that would be kept as the invented one is somebody's own.
            {"location": {"lat": 37.7761, "lon": -122.4239}},
            {"location": FIRE | {"accuracy_m": 12}},
            {"location": FIRE, "clip": clip},
            {"location": FIRE, "photo": clip},
        ):
            refused = client.post("/api/taps", headers=SHOWN, json=body)
            assert (refused.status_code, refused.json()) == (
                403,
                {"detail": front.TAP_REFUSED},
            )
            assert not any(part in refused.text for part in PRECISE)
        # Nothing of a refused tap was kept: no row, and no file of its media.
        assert tap_rows(client) == 2
        media = Path(os.environ["BEACON_DATA_DIR"]) / "taps"
        assert [file.name for file in media.rglob("*") if file.is_file()] in ([], [".lock"])


def test_a_tap_an_earlier_server_took_from_where_somebody_was_is_answered_for_by_no_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its id would be asked for at an address the host can write down.
    with TestClient(create_app()) as client:
        own = client.post("/api/taps", headers=SHOWN, json={"location": OWN}).json()["id"]
        near = client.post("/api/taps", headers=SHOWN, json={"location": FIRE}).json()["id"]
        measured = client.post(
            "/api/taps", headers=SHOWN, json={"location": FIRE | {"accuracy_m": 9}}
        ).json()["id"]
        assert client.get(f"/api/taps/{own}").status_code == 200

    monkeypatch.setenv(front.VARIABLE, "unverified")
    with TestClient(create_app()) as client:
        for gone in (own, measured):
            for route in (f"/api/taps/{gone}", f"/api/taps/{gone}/run"):
                answer = client.get(route)
                assert (answer.status_code, answer.json()) == (
                    404,
                    {"detail": "There is no such tap."},
                )
        assert client.get(f"/api/taps/{near}").status_code == 200


@pytest.mark.usefixtures("unverified")
def test_saved_places_are_checked_only_when_each_is_an_invented_place() -> None:
    with TestClient(create_app()) as client:
        asked = client.post("/api/places/check", headers=SHOWN, json={"places": [FIRE, ACROSS]})
        assert asked.status_code == 200
        assert len(asked.json()["places"]) == 2

        for places in ([OWN], [FIRE, OWN]):
            refused = client.post("/api/places/check", headers=SHOWN, json={"places": places})
            assert (refused.status_code, refused.json()) == (
                403,
                {"detail": front.PLACES_REFUSED},
            )
            assert not any(part in refused.text for part in PRECISE)


@pytest.mark.usefixtures("unverified")
def test_no_notification_address_is_taken_and_one_is_still_deleted() -> None:
    with TestClient(create_app()) as client:
        push = client.app.state.push  # type: ignore[attr-defined]
        assert (push.sends, push.off) == (False, front.PUSH_OFF_REASON)
        info = client.get("/api/push").json()
        assert (info["enabled"], info["text"], info["public_key"]) == (
            False,
            front.PUSH_OFF,
            None,
        )
        for route, body in (
            ("/api/push/permit", {"revoke": []}),
            ("/api/push/subscribe", {"endpoint": "https://fcm.googleapis.com/fcm/send/x"}),
        ):
            refused = client.post(route, headers=SHOWN, json=body)
            assert (refused.status_code, refused.json()) == (503, {"detail": front.PUSH_OFF})
        # What an earlier server kept is deleted when its device asks, as on any.
        gone = client.post(
            "/api/push/unsubscribe",
            headers=SHOWN,
            json={"endpoint": "https://fcm.googleapis.com/fcm/send/an-address", "permits": []},
        )
        assert gone.status_code == 204


def test_a_server_on_a_real_source_does_not_start_while_its_host_is_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEACON_LIVE_SOURCES", "nws")
    monkeypatch.setenv("BEACON_INGEST", "0")
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["mode"] == "live"

    monkeypatch.setenv(front.VARIABLE, "unverified")
    with (
        pytest.raises(ServerSetupError, match="is for the invented scenario"),
        TestClient(create_app()),
    ):
        pass
    # And with the host checked, it starts and takes a visitor's own.
    monkeypatch.setenv(front.VARIABLE, "verified")
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["input"] == "own"


def test_the_sentences_say_what_is_taken_and_claim_nothing_of_the_host() -> None:
    # What the host keeps is not known, so nothing says it keeps nothing.
    assert "nobody has checked" in front.INVENTED
    assert "two invented places" in front.INVENTED
    assert "may keep" in front.INVENTED_MORE
    assert "holds the tap's id" in front.INVENTED_MORE
    for said in (front.INVENTED, front.INVENTED_MORE, front.PUSH_OFF):
        assert "keeps nothing" not in said
        assert "no record" not in said
