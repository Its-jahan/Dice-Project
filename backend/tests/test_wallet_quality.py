"""Wallet scoring: separating the wallets worth counting from the rest."""

from datetime import datetime, timedelta, timezone

import pytest

from app import db, wallets
from app.config import settings

CHAIN = "ethereum"


def _iso(moment):
    return moment.isoformat(timespec="seconds")


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    with db.connect():  # creates the schema in the fresh file
        pass
    return db


def _cohort(name, wallet_list, *, derived=False):
    watchlist_id = db.create_watchlist(
        name=name,
        chain=CHAIN,
        wallets=wallet_list,
        source_token_address=None,
        notes="",
        min_wallets=3,
        min_wallets_pct=0.0,
        buy_window_hours=48,
        monitor_interval_hours=24.0,
        min_buy_usd=0.0,
        auto_monitor=False,
        ignore_tokens=[],
        derived=derived,
    )
    return watchlist_id


def _signal_with_outcome(token, *, entry, price_24h, fired_at, buyers, bought_at):
    """A fired signal, its recorded outcome, and the buy events behind it."""
    db.record_events(
        [
            {
                "chain": CHAIN,
                "wallet_address": wallet,
                "token_address": token,
                "tx_hash": f"0x{wallet[-6:]}{token[-4:]}",
                "token_symbol": "TKN",
                "amount": 1.0,
                "block_num": 1,
                "seen_at": bought_at[wallet],
                "from_address": "0x" + "1" * 40,
                "is_buy": True,
            }
            for wallet in buyers
        ]
    )
    signal_id, _, _ = db.upsert_pool_signal(
        chain=CHAIN,
        token_address=token,
        token_symbol="TKN",
        wallet_count=len(buyers),
        pool_size=10,
        total_usd=None,
        buyers=[],
        breakdown=[],
    )
    db.record_outcome(
        signal_id=signal_id,
        chain=CHAIN,
        token_address=token,
        token_symbol="TKN",
        wallet_count=len(buyers),
        pool_size=10,
        entry_price=entry,
        entry_liquidity=50_000.0,
        pool_age_hours=4.0,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE signal_outcomes SET fired_at = ?, price_24h = ? WHERE signal_id = ?",
            (fired_at, price_24h, signal_id),
        )
    return signal_id


# ------------------------------------------------------------------ shrinkage


def test_one_lucky_signal_does_not_outrank_a_consistent_wallet():
    """The whole point of the shrinkage: rank skill, not luck."""
    lucky = wallets.score(400.0, 1)      # one 4x
    consistent = wallets.score(80.0, 12)  # twelve solid hits

    assert lucky == 100.0
    assert consistent == 64.0
    assert consistent < lucky  # ...but the gap is a quarter of the raw one


def test_an_unmeasured_wallet_scores_nothing_rather_than_zero():
    # Zero would rank a brand-new wallet above one with a genuine loss.
    assert wallets.score(None, 0) is None
    assert wallets.score(None, 4) is None
    assert wallets.score(-50.0, 4) == -28.6


# ---------------------------------------------------------------- leaderboard


def test_a_wallet_is_scored_on_the_signals_it_bought_into(store):
    now = datetime.now(timezone.utc)
    early = "0x" + "a" * 40
    late = "0x" + "b" * 40
    _cohort("early buyers", [early, late])

    _signal_with_outcome(
        "0x" + "1" * 40,
        entry=1.0,
        price_24h=3.0,                      # +200%
        fired_at=_iso(now - timedelta(hours=10)),
        buyers=[early, late],
        bought_at={
            # Six hours ahead of the signal versus ten minutes ahead.
            early: _iso(now - timedelta(hours=16)),
            late: _iso(now - timedelta(hours=10, minutes=10)),
        },
    )

    board = wallets.leaderboard(CHAIN)
    rows = {row["wallet_address"]: row for row in board["rows"]}

    assert rows[early]["signals"] == 1
    assert rows[early]["median_return"] == 200.0
    assert rows[early]["median_lead_hours"] == 6.0
    # Same token, same return — the lead time is what separates them.
    assert rows[late]["median_lead_hours"] == 0.2
    assert board["scored"] == 2
    assert board["proven"] == 0  # one signal is not a record


def test_a_wallet_that_bought_after_the_signal_is_not_credited(store):
    now = datetime.now(timezone.utc)
    buyer = "0x" + "a" * 40
    follower = "0x" + "b" * 40
    _cohort("cohort", [buyer, follower])

    _signal_with_outcome(
        "0x" + "1" * 40,
        entry=1.0,
        price_24h=2.0,
        fired_at=_iso(now - timedelta(hours=5)),
        buyers=[buyer],
        bought_at={buyer: _iso(now - timedelta(hours=9))},
    )
    # The follower bought an hour *after* the signal fired — that is acting on
    # the signal, not predicting it, and it must not read as skill.
    db.record_events(
        [
            {
                "chain": CHAIN,
                "wallet_address": follower,
                "token_address": "0x" + "1" * 40,
                "tx_hash": "0xlate",
                "token_symbol": "TKN",
                "amount": 1.0,
                "block_num": 2,
                "seen_at": _iso(now - timedelta(hours=4)),
                "from_address": "0x" + "1" * 40,
                "is_buy": True,
            }
        ]
    )

    rows = {row["wallet_address"]: row for row in wallets.leaderboard(CHAIN)["rows"]}
    assert rows[buyer]["signals"] == 1
    assert rows[follower]["signals"] == 0
    assert rows[follower]["score"] is None


def test_a_sprayer_is_flagged_by_its_hit_rate(store):
    now = datetime.now(timezone.utc)
    sprayer = "0x" + "c" * 40
    picker = "0x" + "d" * 40
    _cohort("cohort", [sprayer, picker])

    # The sprayer buys forty tokens; the picker buys only the one below.
    db.record_events(
        [
            {
                "chain": CHAIN,
                "wallet_address": sprayer,
                "token_address": f"0x{index:040x}",
                "tx_hash": f"0xspray{index}",
                "token_symbol": None,
                "amount": 1.0,
                "block_num": index,
                "seen_at": _iso(now - timedelta(hours=30)),
                "from_address": "0x" + "1" * 40,
                "is_buy": True,
            }
            for index in range(1, 41)
        ]
    )

    # One of them signals, and both wallets were in it.
    _signal_with_outcome(
        "0x" + "1".rjust(40, "0"),
        entry=1.0,
        price_24h=1.5,
        fired_at=_iso(now - timedelta(hours=20)),
        buyers=[picker],
        bought_at={picker: _iso(now - timedelta(hours=26))},
    )

    rows = {row["wallet_address"]: row for row in wallets.leaderboard(CHAIN)["rows"]}
    assert rows[sprayer]["tokens_bought"] == 40
    assert rows[sprayer]["hit_rate"] == 2.5      # 1 in 40
    assert rows[sprayer]["sprayer"] is True
    # Same signal, reached by buying one thing rather than forty.
    assert rows[picker]["hit_rate"] == 100.0
    assert rows[picker]["sprayer"] is False


def test_a_wallet_that_always_arrives_at_the_threshold_is_flagged(store):
    """A fine return earned twelve minutes early is a return you cannot act on."""
    now = datetime.now(timezone.utc)
    ahead = "0x" + "a" * 40
    crowd = "0x" + "b" * 40
    _cohort("cohort", [ahead, crowd])

    for index, fired_hours in enumerate([40, 30, 20], start=1):
        _signal_with_outcome(
            f"0x{index:040x}",
            entry=1.0,
            price_24h=2.0,
            fired_at=_iso(now - timedelta(hours=fired_hours)),
            buyers=[ahead, crowd],
            bought_at={
                ahead: _iso(now - timedelta(hours=fired_hours + 8)),
                crowd: _iso(now - timedelta(hours=fired_hours, minutes=12)),
            },
        )

    board = wallets.leaderboard(CHAIN)
    rows = {row["wallet_address"]: row for row in board["rows"]}

    # Identical returns, identical scores — the lead time is the whole story.
    assert rows[ahead]["median_return"] == rows[crowd]["median_return"] == 100.0
    assert rows[ahead]["score"] == rows[crowd]["score"]
    assert rows[crowd]["follower"] is True
    assert rows[ahead]["follower"] is False
    assert board["followers"] == 1


def test_one_late_signal_is_not_enough_to_call_a_wallet_a_follower(store):
    """A threshold guarantees somebody crosses it last; that is not a verdict."""
    now = datetime.now(timezone.utc)
    wallet = "0x" + "a" * 40
    _cohort("cohort", [wallet])

    _signal_with_outcome(
        "0x" + "1" * 40,
        entry=1.0,
        price_24h=2.0,
        fired_at=_iso(now - timedelta(hours=5)),
        buyers=[wallet],
        bought_at={wallet: _iso(now - timedelta(hours=5, minutes=1))},
    )

    row = wallets.leaderboard(CHAIN)["rows"][0]
    assert row["median_lead_hours"] == 0.0
    assert row["follower"] is False  # one signal is an accident, not a habit


def test_wallets_with_no_record_still_appear_with_their_cohort_count(store):
    quiet = "0x" + "e" * 40
    _cohort("first token", [quiet])
    _cohort("second token", [quiet])
    _cohort("auto-built", [quiet], derived=True)

    board = wallets.leaderboard(CHAIN)
    row = board["rows"][0]
    assert row["wallet_address"] == quiet
    # Derived cohorts are excluded, or the set reinforces itself.
    assert row["cohorts"] == 2
    assert row["score"] is None
    assert row["hit_rate"] is None  # no buys recorded: unknown, not zero
    assert board["scored"] == 0
