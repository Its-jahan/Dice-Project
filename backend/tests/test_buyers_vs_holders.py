"""Buyer versus holder classification over a date range."""

import pytest

from app.holders import apply_wallet_filter, build_summary, classify_wallets, parse_rows
from app.models import HoldersRequest, WalletType

TOKEN = "0x1234567890abcdef1234567890abcdef12345678"
NEWCOMER = "0x" + "a" * 40      # held nothing, then bought
TOPPER_UP = "0x" + "b" * 40     # already held some, bought more
HOLDER = "0x" + "c" * 40        # flat across the range
SELLER = "0x" + "d" * 40        # only sold
ROUNDTRIP = "0x" + "e" * 40     # sold, then bought back


def _request(**overrides):
    return HoldersRequest(
        chain="ethereum",
        token_address=TOKEN,
        start_date="2026-07-01",
        end_date="2026-07-03",
        **overrides,
    )


def _row(wallet, day, balance):
    return {"wallet_address": wallet, "day": day, "balance": balance}


#: The baseline day (2026-06-30) is fetched by the query but never exported.
ROWS = [
    # Newcomer: absent on the baseline day, appears inside the range.
    _row(NEWCOMER, "2026-07-02", 500),
    _row(NEWCOMER, "2026-07-03", 500),
    # Topping up: held 100 before, ends on 300.
    _row(TOPPER_UP, "2026-06-30", 100),
    _row(TOPPER_UP, "2026-07-01", 100),
    _row(TOPPER_UP, "2026-07-02", 300),
    _row(TOPPER_UP, "2026-07-03", 300),
    # Untouched throughout.
    _row(HOLDER, "2026-06-30", 900),
    _row(HOLDER, "2026-07-01", 900),
    _row(HOLDER, "2026-07-02", 900),
    _row(HOLDER, "2026-07-03", 900),
    # Only ever sells.
    _row(SELLER, "2026-06-30", 800),
    _row(SELLER, "2026-07-01", 500),
    _row(SELLER, "2026-07-02", 200),
    _row(SELLER, "2026-07-03", 200),
    # Sells then buys back — an increase happened, so this is a buyer.
    _row(ROUNDTRIP, "2026-06-30", 400),
    _row(ROUNDTRIP, "2026-07-01", 100),
    _row(ROUNDTRIP, "2026-07-02", 350),
    _row(ROUNDTRIP, "2026-07-03", 350),
]


def _classify(req=None):
    req = req or _request()
    snapshots, facts = classify_wallets(parse_rows(ROWS, req), req)
    return snapshots, facts, req


def test_both_kinds_of_buying_are_recognised():
    _, facts, _ = _classify()

    # Bought in from nothing, and added to an existing bag: both are buyers.
    assert facts[NEWCOMER]["wallet_type"] is WalletType.buyer
    assert facts[TOPPER_UP]["wallet_type"] is WalletType.buyer


def test_flat_and_shrinking_positions_are_holders():
    _, facts, _ = _classify()

    assert facts[HOLDER]["wallet_type"] is WalletType.holder
    assert facts[SELLER]["wallet_type"] is WalletType.holder


def test_selling_then_buying_back_counts_as_buying():
    _, facts, _ = _classify()

    assert facts[ROUNDTRIP]["wallet_type"] is WalletType.buyer
    assert facts[ROUNDTRIP]["bought_amount"] == pytest.approx(250)  # 100 -> 350


def test_opening_balance_separates_first_time_buyers():
    _, facts, _ = _classify()

    # A newcomer starts from zero; a topper-up does not. That is the
    # difference between "found the token" and "already knew about it".
    assert facts[NEWCOMER]["opening_balance"] == 0
    assert facts[NEWCOMER]["bought_amount"] == pytest.approx(500)
    assert facts[TOPPER_UP]["opening_balance"] == pytest.approx(100)
    assert facts[TOPPER_UP]["bought_amount"] == pytest.approx(200)


def test_the_baseline_day_never_reaches_the_output():
    snapshots, _, req = _classify()

    assert snapshots, "the range itself must still be returned"
    assert min(s.snapshot_date for s in snapshots) == req.start_date
    assert all(s.snapshot_date >= req.start_date for s in snapshots)


def test_a_wallet_holding_from_before_is_not_a_buyer_on_day_one():
    """The whole reason the baseline day is fetched.

    Without it, HOLDER's 900 on 1 July is indistinguishable from a purchase
    made that morning.
    """
    _, facts, _ = _classify()

    assert facts[HOLDER]["opening_balance"] == pytest.approx(900)
    assert facts[HOLDER]["bought_amount"] == 0


# ----------------------------------------------------------------- filtering


def test_filter_keeps_only_buyers():
    req = _request(wallet_filter="buyers")
    snapshots, facts = classify_wallets(parse_rows(ROWS, req), req)

    kept = {s.wallet_address for s in apply_wallet_filter(snapshots, facts, req)}

    assert kept == {NEWCOMER, TOPPER_UP, ROUNDTRIP}


def test_filter_keeps_only_holders():
    req = _request(wallet_filter="holders")
    snapshots, facts = classify_wallets(parse_rows(ROWS, req), req)

    kept = {s.wallet_address for s in apply_wallet_filter(snapshots, facts, req)}

    assert kept == {HOLDER, SELLER}


def test_filter_all_keeps_everything():
    snapshots, facts, req = _classify()

    kept = {s.wallet_address for s in apply_wallet_filter(snapshots, facts, req)}

    assert len(kept) == 5


def test_summary_carries_the_classification():
    snapshots, facts, _ = _classify()

    summary = {s.wallet_address: s for s in build_summary(snapshots, facts)}

    assert summary[NEWCOMER].wallet_type is WalletType.buyer
    assert summary[NEWCOMER].opening_balance == 0
    assert summary[HOLDER].wallet_type is WalletType.holder
    assert summary[HOLDER].opening_balance == pytest.approx(900)


def test_minimum_balance_floor_still_reads_as_buying():
    """Below the minimum is treated as the floor, not as missing data.

    A wallet under the threshold that crosses it has bought, and should not be
    silently reclassified as a long-time holder.
    """
    req = _request(min_balance=50)
    rows = [
        _row(NEWCOMER, "2026-06-30", 10),   # under the floor: dropped
        _row(NEWCOMER, "2026-07-02", 500),
    ]

    _, facts = classify_wallets(parse_rows(rows, req), req)

    assert facts[NEWCOMER]["wallet_type"] is WalletType.buyer
    assert facts[NEWCOMER]["opening_balance"] == 0
