"""Pins for the hosted entry point (buschain/web/cloud.py): the login in front
of the cloud copy, and its refusal to start half-configured. Each test names
the hole it keeps shut. The app behind the login is the ordinary create_app(),
so nothing here re-tests the API itself."""
import base64

import pytest
from fastapi.testclient import TestClient

from buschain.web import cloud

PASSWORD = "correct horse battery staple"


def _basic(credentials: str) -> dict[str, str]:
    token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSCHAIN_DB", str(tmp_path / "cloud.db"))
    monkeypatch.setenv("BUSCHAIN_PASSWORD", PASSWORD)


@pytest.fixture
def client(env):
    with TestClient(cloud.build_app()) as c:
        yield c


def test_right_password_reaches_both_the_api_and_the_page(client):
    # Both halves of the app: an API route and the StaticFiles mount. The
    # username is free text, since only the password is checked.
    for path in ("/api/state", "/", "/app.js"):
        res = client.get(path, headers=_basic(f"anyone:{PASSWORD}"))
        assert res.status_code == 200, path


def test_no_credentials_gets_the_browser_login_prompt_everywhere(client):
    # The static files and FastAPI's built-in /docs are not routes a
    # dependency could guard, which is why the guard is middleware. A
    # mutation is refused as well as a read.
    for res in (client.get("/api/state"), client.get("/"), client.get("/app.js"),
                client.get("/docs"), client.post("/api/settings", json={"horizon": "all"})):
        assert res.status_code == 401, res.request.url
        # Without this header the browser shows the bare 401 and never asks.
        assert res.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize("header", [
    "Basic",                                      # no credentials at all
    "Basic !!!not-base64!!!",
    "Basic " + base64.b64encode(PASSWORD.encode()).decode(),   # no colon
    "Bearer " + base64.b64encode(f"x:{PASSWORD}".encode()).decode(),
])
def test_malformed_header_is_a_401_never_a_500(client, header):
    res = client.get("/api/state", headers={"Authorization": header})
    assert res.status_code == 401


@pytest.mark.parametrize("guess", [
    "wrong password entirely",
    PASSWORD[:-1],          # a prefix of the right one
    PASSWORD + "x",         # the right one with something after it
    PASSWORD.upper(),
])
def test_wrong_password_is_refused(client, guess):
    res = client.get("/api/state", headers=_basic(f"anyone:{guess}"))
    assert res.status_code == 401


@pytest.mark.parametrize("missing", ["BUSCHAIN_DB", "BUSCHAIN_PASSWORD"])
def test_refuses_to_start_half_configured(env, monkeypatch, missing):
    # No password would mean an open app; no DB path would mean a plan on the
    # throwaway root filesystem. Either must fail the deploy, not limp along.
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        cloud.build_app()


def test_refuses_a_placeholder_password(env, monkeypatch):
    monkeypatch.setenv("BUSCHAIN_PASSWORD", "password")
    with pytest.raises(RuntimeError, match="BUSCHAIN_PASSWORD"):
        cloud.build_app()
