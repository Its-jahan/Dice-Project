"""Telegram delivery: chat-id normalisation, channel errors, auto-correction."""

import httpx
import pytest

from app import db
from app import monitor
from app.config import settings


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", None, raising=False)
    db.set_setting("telegram_bot_token", "123456:TEST-TOKEN")


def _mock_transport(monkeypatch, handler):
    """Route every httpx call in monitor through a scripted handler."""
    real_client = httpx.AsyncClient

    def factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(monitor.httpx, "AsyncClient", factory)


# --------------------------------------------------------------- normalising


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://t.me/c/1234567890/12", "-1001234567890"),
        ("t.me/c/1234567890", "-1001234567890"),
        ("https://t.me/mychannel", "@mychannel"),
        ("t.me/s/mychannel", "@mychannel"),
        ("  @mychannel  ", "@mychannel"),
        ("-1001234567890", "-1001234567890"),
        ("987654321", "987654321"),   # a plain user id survives untouched
        ("", ""),
    ],
)
def test_normalize_chat_id(raw, expected):
    assert monitor.normalize_chat_id(raw) == expected


# ------------------------------------------------------------------ sending


def test_send_succeeds_for_a_channel(monkeypatch):
    db.set_setting("telegram_chat_id", "-1001234567890")
    seen = {}

    def capture(request):
        import json as _json

        seen["chat_id"] = _json.loads(request.content)["chat_id"]
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    _mock_transport(monkeypatch, capture)

    assert monitor.asyncio.run(monitor.send_telegram_message("hi")) is None
    assert seen["chat_id"] == "-1001234567890"


def test_bare_channel_number_is_retried_with_the_100_prefix(monkeypatch):
    # The classic mistake: the channel's internal id without -100.
    db.set_setting("telegram_chat_id", "1234567890")
    attempts = []

    def handler(request):
        import json as _json

        chat_id = _json.loads(request.content)["chat_id"]
        attempts.append(chat_id)
        if chat_id == "1234567890":
            return httpx.Response(
                400, json={"ok": False, "description": "Bad Request: chat not found"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    _mock_transport(monkeypatch, handler)

    assert monitor.asyncio.run(monitor.send_telegram_message("hi")) is None
    assert attempts == ["1234567890", "-1001234567890"]
    # The correction is persisted so later runs go straight through.
    assert db.get_setting("telegram_chat_id") == "-1001234567890"


def test_chat_not_found_explains_the_channel_id_format(monkeypatch):
    db.set_setting("telegram_chat_id", "@nope")

    def handler(_request):
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )

    _mock_transport(monkeypatch, handler)
    error = monitor.asyncio.run(monitor.send_telegram_message("hi"))

    assert "chat not found" in error
    assert "-100" in error and "added to the channel" in error


def test_missing_admin_rights_is_explained(monkeypatch):
    db.set_setting("telegram_chat_id", "-1001234567890")

    def handler(_request):
        return httpx.Response(
            400,
            json={
                "ok": False,
                "description": (
                    "Bad Request: not enough rights to send text messages to the chat"
                ),
            },
        )

    _mock_transport(monkeypatch, handler)
    error = monitor.asyncio.run(monitor.send_telegram_message("hi"))

    assert "administrator" in error and "Post Messages" in error


def test_bad_token_is_explained(monkeypatch):
    db.set_setting("telegram_chat_id", "-1001234567890")

    def handler(_request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    _mock_transport(monkeypatch, handler)
    error = monitor.asyncio.run(monitor.send_telegram_message("hi"))

    assert "BotFather" in error


def test_unconfigured_telegram_reports_clearly(monkeypatch):
    db.delete_setting("telegram_bot_token")

    error = monitor.asyncio.run(monitor.send_telegram_message("hi"))

    assert "not configured" in error


def test_describe_bot_returns_username(monkeypatch):
    def handler(request):
        assert request.url.path.endswith("/getMe")
        return httpx.Response(
            200, json={"ok": True, "result": {"username": "dice_signals_bot"}}
        )

    _mock_transport(monkeypatch, handler)

    assert monitor.asyncio.run(monitor.describe_telegram_bot()) == "dice_signals_bot"
