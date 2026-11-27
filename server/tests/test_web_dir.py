"""`BEACON_WEB_DIR`: the built app served from the server's own origin, beside /api."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beacon.main import ServerSetupError, create_app


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory shaped as `pnpm build` leaves `web/dist`."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Beacon</title>")
    (root / "assets" / "index-3f2a9c1b.js").write_text("console.log('app')")
    (root / "sw.js").write_text("// the app's worker")
    (root / "push-sw.js").write_text("// the notifications' worker")
    (root / "manifest.webmanifest").write_text("{}")
    (tmp_path / "secret.txt").write_text("not the app's")
    monkeypatch.setenv("BEACON_WEB_DIR", str(root))
    return root


def test_the_built_app_is_served_at_the_root_and_the_api_beside_it(built: Path) -> None:
    client = TestClient(create_app())

    page = client.get("/")
    assert page.status_code == 200
    assert "<title>Beacon</title>" in page.text
    assert page.headers["content-type"].startswith("text/html")
    # Every route under /api is the server's still, and none is ever a file's.
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/nothing").status_code == 404
    assert client.post("/api/nothing").status_code in (404, 405)


def test_what_changes_with_a_deploy_is_asked_about_each_time_and_the_rest_is_kept(
    built: Path,
) -> None:
    client = TestClient(create_app())

    # The page, both service workers, and the manifest: a browser has them as they
    # were last deployed, the worker that shows a notification among them.
    for path in ("/", "/index.html", "/sw.js", "/push-sw.js", "/manifest.webmanifest"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    # A file whose name holds a digest of what it holds.
    kept = client.get("/assets/index-3f2a9c1b.js")
    assert kept.headers["cache-control"] == "public, max-age=31536000, immutable"
    # What is not there is not said to be kept.
    missing = client.get("/assets/index-00000000.js")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")


def test_nothing_outside_the_built_app_is_served(built: Path) -> None:
    client = TestClient(create_app())

    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "not the app's" not in response.text


def test_with_no_setting_the_server_serves_the_api_alone() -> None:
    client = TestClient(create_app())

    assert client.get("/").status_code == 404
    assert client.get("/api/health").status_code == 200


def test_a_setting_that_names_no_built_app_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEACON_WEB_DIR", str(tmp_path))

    with pytest.raises(ServerSetupError, match=r"names no built app.*pnpm build"):
        create_app()
