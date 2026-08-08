"""Noticing that the pipeline has gone quiet.

This exists because of a real failure: Alchemy stopped delivering for twelve
and a half hours while its webhook was still registered and active, the
service never restarted, and nothing errored. Seventeen hours blind across a
week, and the dashboard was green throughout.

So the tests that matter are about *silence*, which is the one thing a system
built around incoming events cannot notice by itself. Two guarantees are
load-bearing in opposite directions and both are here: it must not stay quiet
when the pipeline is dead, and it must not cry wolf when there is simply
nothing to deliver.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, main, watchdog
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
def pipeline(client, monkeypatch):
    """A configured, live install: a token, a live watchlist, and a re-sync
    that succeeds without touching the network."""
    db.set_setting("alchemy_auth_token", "test-token")
    client.post("/api/watchlists", json={
        "name": "live", "chain": "ethereum",
        "wallets": ["0x" + "11" * 20, "0x" + "22" * 20],
        "realtime": True,
    })
    calls: list[str] = []

    async def _resync():
        calls.append("resync")
        return {"synced": []}

    monkeypatch.setattr(watchdog, "_resync", _resync)
    # The process-start floor would otherwise mask every simulated outage.
    monkeypatch.setattr(
        watchdog, "STARTED_AT", datetime.now(timezone.utc) - timedelta(days=1)
    )
    return calls


def _delivered(minutes_ago: float) -> None:
    """Pretend a delivery arrived that long ago.

    Written through :func:`app.db.record_delivery` and then back-dated, rather
    than with a hand-rolled INSERT. A hand-rolled one encodes a guess at the
    schema, and a fixture that guesses is how the exit-warning bug survived
    its own tests.
    """
    db.record_delivery(chain="ethereum", status="ok", activity_count=1, stored=1)
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with db.connect() as conn:
        conn.execute(
            "UPDATE webhook_deliveries SET received_at = ? "
            "WHERE id = (SELECT MAX(id) FROM webhook_deliveries)",
            (when.isoformat(timespec="seconds"),),
        )


def _sent(monkeypatch) -> list[str]:
    messages: list[str] = []

    async def _capture(text: str) -> None:
        messages.append(text)

    monkeypatch.setattr(main.monitor, "send_watchdog_notification", _capture)
    monkeypatch.setattr(watchdog.monitor, "send_watchdog_notification", _capture)
    return messages


# ------------------------------------------------------------------- healthy


@pytest.mark.anyio
async def test_a_flowing_pipeline_is_left_alone(pipeline):
    _delivered(0.5)
    result = await watchdog.check()
    assert result["healthy"] is True
    assert pipeline == []          # no re-sync
    assert db.open_outage() is None


# ------------------------------------------------------------------ the fault


@pytest.mark.anyio
async def test_silence_triggers_a_resync_before_anyone_is_told(pipeline, monkeypatch):
    """Re-sync first, shout later. The twelve-hour outage was fixed by exactly
    this call, so it should happen without waking anybody."""
    messages = _sent(monkeypatch)
    _delivered(25)   # past silence (20), short of alert (45)

    result = await watchdog.check()
    assert result["healthy"] is False
    assert pipeline == ["resync"]
    assert messages == []                       # deliberately quiet
    assert db.open_outage() is not None


@pytest.mark.anyio
async def test_a_long_silence_is_escalated(pipeline, monkeypatch):
    messages = _sent(monkeypatch)
    _delivered(60)   # past the alert threshold

    await watchdog.check()
    assert len(messages) == 1
    assert "not receiving webhook deliveries" in messages[0]
    # The message must say recovery was already attempted, or it reads as
    # something the operator has to go and do by hand.
    assert "re-synced" in messages[0]
    # ...and it must say how many times, correctly. Composing this from the
    # row as it was read *before* the re-sync made it claim "0 time(s)" in the
    # same breath as having just re-synced.
    assert "0 time(s)" not in messages[0]
    assert "1 time(s)" in messages[0]


@pytest.mark.anyio
async def test_it_shouts_once_not_every_sweep(pipeline, monkeypatch):
    """The sweep runs every minute. Without the claim, a night-long outage is
    600 identical messages and the next real one is ignored."""
    messages = _sent(monkeypatch)
    _delivered(60)

    for _ in range(5):
        await watchdog.check()

    assert len(messages) == 1


@pytest.mark.anyio
async def test_resyncs_back_off_rather_than_hammering(pipeline, monkeypatch):
    """A provider outage lasting a day must not mean 144 attempts against an
    API that is plainly not answering."""
    _sent(monkeypatch)
    _delivered(60)

    for _ in range(6):
        await watchdog.check()

    # Cooldown starts at 10 minutes and doubles, so within one simulated
    # moment only the first attempt is due.
    assert pipeline == ["resync"]


@pytest.mark.anyio
async def test_a_failing_resync_still_counts_as_an_attempt(pipeline, monkeypatch):
    """Otherwise a provider that always errors is retried every single sweep,
    which is the loop this whole feature exists to avoid."""
    _sent(monkeypatch)

    async def _explode():
        raise RuntimeError("alchemy is down")

    monkeypatch.setattr(watchdog, "_resync", _explode)
    _delivered(60)

    await watchdog.check()
    await watchdog.check()
    outage = db.open_outage()
    assert outage["resyncs"] == 1          # attempted once, not twice
    assert outage["last_resync_at"] is not None


@pytest.mark.anyio
async def test_the_watchdog_never_breaks_the_sweep(pipeline, monkeypatch):
    """It is the thing that reports breakage; it must not become breakage."""
    def _explode():
        raise RuntimeError("db is unhappy")

    monkeypatch.setattr(db, "last_delivery_at", _explode)
    with pytest.raises(RuntimeError):
        await watchdog.check()      # raises here...

    from app import realtime
    await realtime.sweep()          # ...but the sweep itself is unaffected


# --------------------------------------------------------------- the real one


@pytest.mark.anyio
@pytest.mark.parametrize(
    "minutes_silent, expect_resync, expect_alert",
    [
        (5, False, False),      # a normal quiet stretch
        (19, False, False),     # still under the threshold
        (21, True, False),      # re-sync, silently
        (46, True, True),       # re-sync did not help — say so
        (750, True, True),      # the twelve and a half hours that actually happened
    ],
)
async def test_the_outage_that_happened_is_caught_within_twenty_minutes(
    pipeline, monkeypatch, minutes_silent, expect_resync, expect_alert
):
    """Replays 2026-08-08: last delivery 23:17, next 11:49 — 752 minutes of
    silence that nothing noticed. At every point along that line the watchdog
    now does something, and it does it inside the first twenty minutes rather
    than the first twelve hours.
    """
    messages = _sent(monkeypatch)
    _delivered(minutes_silent)

    await watchdog.check()

    assert bool(pipeline) is expect_resync
    assert bool(messages) is expect_alert


# ------------------------------------------------------------------ recovery


@pytest.mark.anyio
async def test_recovery_closes_the_outage_and_says_so(pipeline, monkeypatch):
    messages = _sent(monkeypatch)
    _delivered(60)
    await watchdog.check()
    assert len(messages) == 1

    _delivered(0)                    # deliveries resume
    result = await watchdog.check()

    assert result["recovered"] is True
    assert db.open_outage() is None
    assert "arriving again" in messages[1]


@pytest.mark.anyio
async def test_a_self_healed_blip_needs_no_all_clear(pipeline, monkeypatch):
    """If nobody was told it broke, nobody needs telling it is fixed."""
    messages = _sent(monkeypatch)
    _delivered(25)                   # re-synced, never alerted
    await watchdog.check()

    _delivered(0)
    await watchdog.check()

    assert messages == []
    assert db.open_outage() is None


# --------------------------------------------------------------- not a fault


@pytest.mark.anyio
async def test_an_install_with_nothing_live_is_not_broken(client, monkeypatch):
    """No live watchlist means nothing *should* arrive. Alarming here would
    teach the operator that the alarm means nothing."""
    db.set_setting("alchemy_auth_token", "test-token")
    messages = _sent(monkeypatch)
    monkeypatch.setattr(
        watchdog, "STARTED_AT", datetime.now(timezone.utc) - timedelta(days=1)
    )

    result = await watchdog.check()
    assert result["checked"] is False
    assert messages == []


@pytest.mark.anyio
async def test_an_install_with_no_alchemy_token_is_not_broken(client, monkeypatch):
    messages = _sent(monkeypatch)
    result = await watchdog.check()
    assert result["checked"] is False
    assert messages == []


@pytest.mark.anyio
async def test_a_brand_new_install_is_given_time_to_receive_its_first_delivery(
    client, monkeypatch
):
    """Nothing has ever arrived, so there is no silence to measure yet — the
    webhook may have been created seconds ago. STARTED_AT is the floor only
    in this case."""
    db.set_setting("alchemy_auth_token", "test-token")
    client.post("/api/watchlists", json={
        "name": "live", "chain": "ethereum",
        "wallets": ["0x" + "11" * 20], "realtime": True,
    })
    messages = _sent(monkeypatch)
    monkeypatch.setattr(watchdog, "STARTED_AT", datetime.now(timezone.utc))

    result = await watchdog.check()
    assert result["healthy"] is True
    assert messages == []


@pytest.mark.anyio
async def test_a_restart_does_not_make_an_old_silence_look_young(pipeline):
    """The tempting bug: floor the clock at process start so deploys stay
    quiet, and a twelve-hour outage reports itself as twenty minutes. Restart
    mid-outage and the number must still be the real one."""
    _delivered(752)                       # the outage that actually happened
    watchdog_started_now = datetime.now(timezone.utc)
    import app.watchdog as module
    module.STARTED_AT = watchdog_started_now

    reported = watchdog.status()["silent_for_minutes"]
    assert reported > 700, f"understated the outage as {reported} minutes"


@pytest.mark.anyio
async def test_it_can_be_switched_off(pipeline, monkeypatch):
    messages = _sent(monkeypatch)
    db.set_setting("watchdog_enabled", "0")
    _delivered(600)

    assert (await watchdog.check())["checked"] is False
    assert messages == []


# ------------------------------------------------------------------- the API


def test_health_reports_silence_without_changing_anything(client, pipeline):
    _delivered(30)
    body = client.get("/api/realtime/health").json()
    assert body["healthy"] is False
    assert body["silent_for_minutes"] >= 29
    assert db.open_outage() is None      # a GET must not open an outage


def test_the_thresholds_are_tunable(client, pipeline):
    response = client.put(
        "/api/settings/watchdog",
        json={"silence_minutes": 5, "alert_minutes": 30},
    )
    assert response.status_code == 200
    assert response.json()["silence_minutes"] == 5


def test_alerting_sooner_than_resyncing_is_refused(client, pipeline):
    """The gap between them is the whole design: automatic recovery gets a
    chance before a human is involved."""
    response = client.put(
        "/api/settings/watchdog",
        json={"silence_minutes": 30, "alert_minutes": 10},
    )
    assert response.status_code == 422


def test_outage_history_survives_for_inspection(client, pipeline):
    db.start_outage(detected_at="2026-08-08T00:00:00+00:00",
                    last_delivery_at="2026-08-07T23:17:14+00:00")
    body = client.get("/api/realtime/health").json()
    assert len(body["recent_outages"]) == 1
    assert body["recent_outages"][0]["last_delivery_at"] == "2026-08-07T23:17:14+00:00"
