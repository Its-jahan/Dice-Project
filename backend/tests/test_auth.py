"""The front door.

The valuable tests here are the ones that fail *open*: a gate that accidentally
locks out the owner, or one that silently blocks the webhooks, is worse than no
gate at all. The second of those has already happened once on this deployment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "live_sweep_seconds", 3600)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def signed_out(client):
    """A client with a password set and nobody signed in."""
    client.put("/api/auth/password", json={"password": "correct-horse-battery"})
    client.cookies.clear()
    return client


def _sign_in(client, password: str = "correct-horse-battery", **extra):
    return client.post(
        "/login", data={"password": password, "next": "/", **extra},
        follow_redirects=False,
    )


# ------------------------------------------------------------ the gate is off


def test_an_install_without_a_password_is_not_locked_out(client):
    """Upgrading must never lock someone out of their own server."""
    assert client.get("/api/auth/status").json()["password_set"] is False
    assert client.get("/api/signals").status_code == 200


# ----------------------------------------------------------- the gate is on


def test_the_api_answers_401_rather_than_redirecting(signed_out):
    """A fetch() cannot follow a redirect to HTML and report anything useful."""
    assert signed_out.get("/api/signals").status_code == 401


def test_a_page_redirects_and_remembers_where_you_were_going(signed_out):
    response = signed_out.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/settings"


def test_the_webhooks_stay_open_without_a_cookie(signed_out):
    """The one that matters.

    Alchemy cannot present a cookie. Gating this path stops every signal and
    looks exactly like a working system, because nothing errors anywhere the
    owner can see it.
    """
    response = signed_out.post("/api/webhooks/alchemy", json={})
    assert response.status_code != 401
    assert response.status_code != 303


def test_the_login_page_itself_is_reachable(signed_out):
    response = signed_out.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


# --------------------------------------------------------------- signing in


def test_the_right_password_opens_the_door(signed_out):
    response = _sign_in(signed_out)
    assert response.status_code == 303
    assert auth.SESSION_COOKIE in signed_out.cookies
    assert signed_out.get("/api/signals").status_code == 200


def test_the_wrong_password_does_not(signed_out):
    response = _sign_in(signed_out, "guess")
    assert response.status_code == 401
    assert "Incorrect password" in response.text
    assert auth.SESSION_COOKIE not in signed_out.cookies


def test_the_session_cookie_is_not_readable_by_javascript(signed_out):
    """HttpOnly is what stops an XSS from becoming a stolen session."""
    response = _sign_in(signed_out)
    assert "httponly" in response.headers["set-cookie"].lower()


def test_signing_out_invalidates_the_session_server_side(signed_out):
    _sign_in(signed_out)
    token = signed_out.cookies[auth.SESSION_COOKIE]
    signed_out.post("/api/auth/logout")

    # Not merely cleared in the browser — replaying the token must fail too.
    assert auth.session_is_valid(token) is False


def test_a_forged_cookie_is_refused(signed_out):
    signed_out.cookies.set(auth.SESSION_COOKIE, "made-up")
    assert signed_out.get("/api/signals").status_code == 401


def test_changing_the_password_signs_everyone_out(signed_out):
    _sign_in(signed_out)
    signed_out.put("/api/auth/password", json={"password": "a-different-one"})
    assert signed_out.get("/api/signals").status_code == 401


def test_a_short_password_is_refused(client):
    assert client.put("/api/auth/password", json={"password": "abc"}).status_code == 422


# --------------------------------------------------------------- the throttle


def test_repeated_guesses_are_locked_out(signed_out):
    for _ in range(auth.MAX_ATTEMPTS):
        assert _sign_in(signed_out, "guess").status_code == 401

    # The next attempt is refused before the password is even considered.
    response = _sign_in(signed_out, "guess")
    assert response.status_code == 429
    assert "Try again in" in response.text

    # And the lockout is not a password check, so the right one is refused too.
    assert _sign_in(signed_out).status_code == 429


def test_a_correct_password_wipes_the_failures(signed_out):
    for _ in range(auth.MAX_ATTEMPTS - 1):
        _sign_in(signed_out, "guess")
    assert _sign_in(signed_out).status_code == 303

    signed_out.post("/api/auth/logout")
    # One bad day must not accumulate into a lockout on the next one.
    assert _sign_in(signed_out, "guess").status_code == 401


def test_the_lockout_survives_a_restart(signed_out):
    """An in-memory counter would reset here, and 'restart the process' is not
    a lock anyone should be able to pick."""
    for _ in range(auth.MAX_ATTEMPTS):
        _sign_in(signed_out, "guess")

    from importlib import reload

    reload(auth)
    assert auth.lockout_remaining("testclient") > 0


# ------------------------------------------------------------- open redirects


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "javascript:alert(1)"])
def test_the_next_parameter_cannot_leave_the_site(signed_out, target):
    response = _sign_in(signed_out, next=target)
    assert response.headers["location"] == "/"
