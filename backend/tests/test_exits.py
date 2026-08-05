"""Warning that the cohort which got you in is getting out.

The tests that matter are the ones about *not* alerting. An exit warning that
fires on one wallet rotating, or fires again every minute, is worse than none:
it teaches the operator to swipe the notification away, and the next one they
swipe away is the one that mattered.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db, exits, main
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore
from app.models import TokenBuyer

CHAIN = "ethereum"
TOKEN = "0x" + "aa" * 20


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


def _wallets(count: int) -> list[str]:
    return [f"0x{i:040x}" for i in range(1, count + 1)]


def _signal(client, buyers: list[str], *, fired_at: str = "2026-08-01T00:00:00+00:00") -> int:
    """A signal row shaped exactly as the app writes them.

    The ``buyers`` column holds serialised TokenBuyer *objects*. A fixture that
    wrote bare address strings passed against code that read bare address
    strings — and both were wrong about production, where the exit count would
    have been zero forever. The record is built through the real model so the
    test cannot drift from the schema again.
    """
    records = [
        TokenBuyer(
            wallet_address=wallet,
            buy_count=1,
            amount_usd=None,
            first_buy_at=fired_at,
            last_buy_at=fired_at,
            via="live",
        ).model_dump()
        for wallet in buyers
    ]
    with db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO signals (chain, token_address, token_symbol, wallet_count,
                                 watchlist_size, buyers, first_seen_at,
                                 last_updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (CHAIN, TOKEN, "TEST", len(buyers), 100, json.dumps(records),
             fired_at, fired_at),
        )
        return int(cursor.lastrowid)


def _sold(wallets: list[str], *, at: str = "2026-08-02T00:00:00+00:00") -> None:
    db.record_events([
        {
            "chain": CHAIN,
            "wallet_address": wallet,
            "token_address": TOKEN,
            "tx_hash": f"0x{i:064x}",
            "token_symbol": "TEST",
            "amount": 1.0,
            "block_num": 1,
            "seen_at": at,
            "from_address": wallet,
            "is_buy": False,
            "is_sell": True,
        }
        for i, wallet in enumerate(wallets)
    ])


# --------------------------------------------------------------- the counting


def test_it_counts_only_the_wallets_named_on_the_signal(client):
    """The pool is thousands of wallets. Whether *those* sold is a different
    question from whether the ten that triggered this signal sold."""
    buyers = _wallets(10)
    signal_id = _signal(client, buyers)
    _sold(buyers[:4])
    # A wallet that never bought this signal sells too — must not count.
    _sold(["0x" + "ff" * 20])

    measured = exits.measure(db.get_signal(signal_id))
    assert measured["sellers"] == 4
    assert measured["buyers"] == 10
    assert measured["pct"] == 40.0


def test_a_sale_before_the_signal_is_not_an_exit(client):
    """Selling last week and buying back in is not leaving. Without the
    timestamp floor the wallet's own history condemns the position."""
    buyers = _wallets(10)
    signal_id = _signal(client, buyers, fired_at="2026-08-05T00:00:00+00:00")
    _sold(buyers[:5], at="2026-08-01T00:00:00+00:00")  # before it fired

    assert exits.measure(db.get_signal(signal_id))["sellers"] == 0


def test_a_signal_with_no_recorded_buyers_is_unknown_not_intact(client):
    """None, not zero. Reporting '0% have exited' for a signal nobody can
    measure is a reassurance that was never checked."""
    signal_id = _signal(client, [])
    assert exits.measure(db.get_signal(signal_id)) is None


# ------------------------------------------------------------ what is news


def test_one_wallet_leaving_is_not_news(client):
    """A third of three buyers is one wallet, and one wallet closing a
    position is a Tuesday. The floor exists for exactly this."""
    buyers = _wallets(3)
    signal_id = _signal(client, buyers)
    _sold(buyers[:1])

    measured = exits.measure(db.get_signal(signal_id))
    assert measured["pct"] > exits.exit_pct()   # 33.3% clears the percentage
    assert exits.is_news(measured) is False     # ...and the floor still stops it


def test_a_few_leaving_a_large_cohort_is_not_news(client):
    """Three of fifty is 6%: the floor is met, the percentage is not. Both
    bars have to hold or the other one is decoration."""
    buyers = _wallets(50)
    signal_id = _signal(client, buyers)
    _sold(buyers[:3])

    assert exits.is_news(exits.measure(db.get_signal(signal_id))) is False


def test_a_third_of_a_real_cohort_leaving_is_news(client):
    buyers = _wallets(12)
    signal_id = _signal(client, buyers)
    _sold(buyers[:4])

    assert exits.is_news(exits.measure(db.get_signal(signal_id))) is True


# ------------------------------------------------------------- the alerting


@pytest.mark.anyio
async def test_it_warns_once_and_then_stays_quiet(client):
    """The whole point of the table. The sweep runs every minute; without
    this the same warning goes out 1,440 times a day."""
    buyers = _wallets(12)
    _signal(client, buyers)
    _sold(buyers[:6])

    first = await exits.check()
    assert first["alerted"] == 1

    second = await exits.check()
    assert second["alerted"] == 0
    # ...and it is not merely un-sent, it is no longer even a candidate.
    assert second["checked"] == 0


@pytest.mark.anyio
async def test_an_old_signal_is_not_worth_warning_about(client):
    """Past the window a signal is not a position, and a warning about it
    would be archaeology."""
    buyers = _wallets(12)
    _signal(client, buyers, fired_at="2020-01-01T00:00:00+00:00")
    _sold(buyers[:6])

    assert (await exits.check())["alerted"] == 0


@pytest.mark.anyio
async def test_a_dismissed_signal_is_left_alone(client):
    """The operator already judged this trade; telling them it went bad is
    noise, and the same reasoning already governs briefs."""
    buyers = _wallets(12)
    signal_id = _signal(client, buyers)
    _sold(buyers[:6])
    with db.connect() as conn:
        conn.execute("UPDATE signals SET status='dismissed' WHERE id=?", (signal_id,))

    assert (await exits.check())["alerted"] == 0


@pytest.mark.anyio
async def test_no_telegram_configured_is_not_an_error(client):
    """Same guarantee as everywhere else: with the feature unconfigured the
    system behaves exactly as if it were absent."""
    buyers = _wallets(12)
    _signal(client, buyers)
    _sold(buyers[:6])

    result = await exits.check()   # no bot token in this fixture
    assert result["alerted"] == 1  # recorded, just not delivered


# ------------------------------------------------------------------ the API


def test_the_threshold_is_tunable_without_a_redeploy(client):
    response = client.put("/api/settings/exits", json={"exit_pct": 50.0})
    assert response.status_code == 200
    assert response.json()["exit_pct"] == 50.0
    assert exits.exit_pct() == 50.0


@pytest.mark.parametrize(
    "body",
    [{"exit_pct": 0}, {"exit_pct": 101}, {"exit_min_wallets": 0}, {"exit_window_days": 0}],
)
def test_nonsense_settings_are_refused(client, body):
    assert client.put("/api/settings/exits", json=body).status_code == 422


def test_the_signal_endpoint_still_reads_a_signal_we_wrote(client):
    """Guards the fixture itself. If _signal() drifts from the real schema
    again the API that parses these rows is the thing that notices."""
    _signal(client, _wallets(3))
    assert client.get("/api/signals").status_code == 200


def test_the_exits_endpoint_reports_what_was_warned_about(client):
    buyers = _wallets(12)
    signal_id = _signal(client, buyers)
    db.record_exit(
        signal_id=signal_id, sellers=6, buyers=12, pct=50.0,
        alerted_at="2026-08-04T00:00:00+00:00",
    )
    body = client.get("/api/signals/exits").json()
    assert len(body["exits"]) == 1
    assert body["exits"][0]["pct"] == 50.0
    assert body["exits"][0]["token_symbol"] == "TEST"
