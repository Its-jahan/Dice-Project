"""Server-side key storage, notification settings and Dune maintenance."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main
from app import monitor as monitor_module
from app.cache import DiskCache
from app.config import settings
from app.dune import DuneAuthError, DuneError
from app.jobs import JobStore

WALLET = "0x" + "a" * 40


class FakeDuneClient:
    valid_key = True
    queries: list[dict] = []
    archived: list[int] = []
    unarchivable: set[int] = set()
    calls: dict = {}

    def __init__(self, api_key, **_kwargs):
        if not api_key or not api_key.strip():
            raise DuneAuthError("no Dune API key supplied")
        FakeDuneClient.calls["api_key"] = api_key
        self.key_fingerprint = "fp-" + api_key.strip()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def validate_key(self):
        return FakeDuneClient.valid_key

    async def list_queries(self, *, limit=100, offset=0):
        page = FakeDuneClient.queries[offset : offset + limit]
        return page, len(FakeDuneClient.queries)

    async def archive_query(self, query_id):
        if query_id in FakeDuneClient.unarchivable:
            raise DuneError(f"Dune returned 403: query {query_id} is not yours")
        FakeDuneClient.archived.append(query_id)


@pytest.fixture
def client(monkeypatch, tmp_path):
    FakeDuneClient.valid_key = True
    FakeDuneClient.queries = []
    FakeDuneClient.archived = []
    FakeDuneClient.unarchivable = set()
    FakeDuneClient.calls = {}
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    monkeypatch.setattr(main, "DuneClient", FakeDuneClient)
    monkeypatch.setattr(monitor_module, "DuneClient", FakeDuneClient)
    with TestClient(main.app) as test_client:
        yield test_client


# -------------------------------------------------------------- key storage


def test_save_key_validates_stores_and_replaces(client):
    assert client.get("/api/config").json()["server_key_configured"] is False

    saved = client.post("/api/key", json={"key": "first-key-1234"})
    assert saved.status_code == 200
    assert saved.json()["hint"] == "…1234"
    assert FakeDuneClient.calls["api_key"] == "first-key-1234"

    config = client.get("/api/config").json()
    assert config["server_key_configured"] is True
    assert config["server_key_hint"] == "…1234"
    assert "first-key" not in str(config)  # never echoed in full
    assert config["monitor"]["auto_possible"] is False  # monitor disabled in tests

    # Saving another key replaces the old one.
    client.post("/api/key", json={"key": "second-key-5678"})
    assert client.get("/api/config").json()["server_key_hint"] == "…5678"
    assert db.server_api_key() == "second-key-5678"


def test_invalid_key_is_not_saved(client):
    FakeDuneClient.valid_key = False

    response = client.post("/api/key", json={"key": "bad-key-0000"})

    assert response.status_code == 401
    assert client.get("/api/config").json()["server_key_configured"] is False


def test_saved_key_is_used_when_no_header_is_sent(client):
    client.post("/api/key", json={"key": "stored-key-9999"})
    FakeDuneClient.calls = {}

    # No X-Dune-Api-Key header: the stored key must be picked up.
    response = client.post("/api/key/validate")

    assert response.status_code == 200
    assert FakeDuneClient.calls["api_key"] == "stored-key-9999"


def test_clear_key(client):
    client.post("/api/key", json={"key": "stored-key-9999"})
    assert client.delete("/api/key").status_code == 200
    assert client.get("/api/config").json()["server_key_configured"] is False
    assert client.post("/api/key/validate").status_code == 401


# ------------------------------------------------------------- notifications


def test_notification_settings_roundtrip_and_masking(client):
    empty = client.get("/api/settings/notifications").json()
    assert empty["telegram_configured"] is False

    saved = client.put(
        "/api/settings/notifications",
        json={"bot_token": "123456:ABC-secret-token", "chat_id": "424242"},
    ).json()

    assert saved["telegram_configured"] is True
    assert saved["chat_id"] == "424242"
    assert saved["bot_token_hint"] == "…oken"
    assert "secret" not in str(saved)

    config = client.get("/api/config").json()
    assert config["monitor"]["telegram_configured"] is True

    # Clearing the chat id turns notifications off.
    cleared = client.put(
        "/api/settings/notifications", json={"chat_id": ""}
    ).json()
    assert cleared["telegram_configured"] is False


def test_notification_test_endpoint_reports_send_result(client, monkeypatch):
    async def ok(_text):
        return None

    async def fail(_text):
        return "Telegram answered 403: bot blocked"

    monkeypatch.setattr(monitor_module, "send_telegram_message", ok)
    client.put(
        "/api/settings/notifications",
        json={"bot_token": "123:abc", "chat_id": "-1001234567890"},
    )

    sent = client.post("/api/settings/notifications/test").json()
    assert sent["sent"] is True
    # The response echoes where it landed, so a corrected id is visible.
    assert sent["chat_id"] == "-1001234567890"

    monkeypatch.setattr(monitor_module, "send_telegram_message", fail)
    response = client.post("/api/settings/notifications/test")
    assert response.status_code == 502
    assert "403" in response.json()["detail"]


def test_saving_a_tme_link_stores_the_channel_id(client):
    saved = client.put(
        "/api/settings/notifications",
        json={"bot_token": "123:abc", "chat_id": "https://t.me/c/1234567890/8"},
    ).json()

    assert saved["chat_id"] == "-1001234567890"


# --------------------------------------------------------- dune maintenance


def test_archive_queries_touches_only_dice_queries(client):
    FakeDuneClient.queries = [
        {"id": 1, "name": "DICE holders ethereum 0x12345 2026-07-20..2026-07-22"},
        {"id": 2, "name": "my own precious query"},
        {"id": 3, "name": "DICE monitor: early buyers (ethereum)"},
        {"id": 4, "name": "DICE catalogue ethereum"},
    ]
    # A stored slot for this account must be forgotten after archiving.
    db.set_query_slot("fp-k", "holders", 1)

    response = client.post(
        "/api/dune/archive-queries", headers={"X-Dune-Api-Key": "k"}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["found"] == 3
    assert body["archived"] == 3
    assert body["failed"] == 0
    assert body["slots_cleared"] == 1
    assert FakeDuneClient.archived == [1, 3, 4]
    assert db.get_query_slot("fp-k", "holders") is None


def test_archive_reports_why_queries_failed(client):
    FakeDuneClient.queries = [
        {"id": 7, "name": "DICE holders ethereum"},
        {"id": 8, "name": "DICE monitor: list"},
    ]
    FakeDuneClient.unarchivable = {7, 8}
    db.set_query_slot("fp-k", "holders", 7)

    body = client.post(
        "/api/dune/archive-queries", headers={"X-Dune-Api-Key": "k"}
    ).json()

    assert body["archived"] == 0
    assert body["failed"] == 2
    assert "403" in body["errors"][0]  # the reason reaches the caller
    # Nothing was archived, so the reuse slot must survive — dropping it would
    # make the next run create yet another query against a full cap.
    assert body["slots_cleared"] == 0
    assert db.get_query_slot("fp-k", "holders") == 7


def test_archive_uses_post_not_patch():
    """Dune archives via POST; PATCH answers 404 and silently archives nothing."""
    import inspect

    from app.dune import DuneClient

    source = inspect.getsource(DuneClient.archive_query)
    assert '"POST"' in source


def test_archive_queries_requires_a_key(client):
    assert client.post("/api/dune/archive-queries").status_code == 401
