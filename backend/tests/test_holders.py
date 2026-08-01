from datetime import date, timedelta

import pytest

from app.holders import apply_holder_mode, build_summary, parse_rows
from app.models import HolderMode, HoldersRequest

TOKEN = "0x1234567890abcdef1234567890abcdef12345678"
WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def make_request(**overrides):
    payload = {
        "chain": "ethereum",
        "token_address": TOKEN,
        "start_date": "2026-07-20",
        "end_date": "2026-07-22",
    }
    payload.update(overrides)
    return HoldersRequest(**payload)


def test_parse_rows_accepts_dune_date_and_numeric_formats():
    req = make_request()
    rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20 00:00:00.000 UTC", "balance": "1540.25"},
        {"address": WALLET_B, "snapshot_date": "2026-07-21T00:00:00Z", "balance": 82.1},
    ]

    snapshots = parse_rows(rows, req)

    assert [s.wallet_address for s in snapshots] == [WALLET_A, WALLET_B]
    assert snapshots[0].snapshot_date == date(2026, 7, 20)
    assert snapshots[0].balance == pytest.approx(1540.25)
    # token_address falls back to the requested token when Dune omits it
    assert snapshots[1].token_address == TOKEN


def test_parse_rows_drops_unusable_and_out_of_range_rows():
    req = make_request(min_balance=100)
    rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 150},
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 5},       # below min
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 0},       # zero
        {"wallet_address": WALLET_A, "day": "2026-07-25", "balance": 900},     # outside range
        {"wallet_address": WALLET_A, "day": "not-a-date", "balance": 900},     # bad date
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": "n/a"},   # bad number
        {"day": "2026-07-20", "balance": 900},                                 # no wallet
    ]

    snapshots = parse_rows(rows, req)

    assert len(snapshots) == 1
    assert snapshots[0].balance == 150


def test_continuous_mode_keeps_only_wallets_present_every_day():
    req = make_request(holder_mode="continuous")
    rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 10},
        {"wallet_address": WALLET_A, "day": "2026-07-21", "balance": 11},
        {"wallet_address": WALLET_A, "day": "2026-07-22", "balance": 12},
        {"wallet_address": WALLET_B, "day": "2026-07-21", "balance": 50},
    ]

    snapshots = apply_holder_mode(parse_rows(rows, req), req)

    assert {s.wallet_address for s in snapshots} == {WALLET_A}


def test_daily_and_any_time_modes_keep_every_wallet_seen():
    rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 10},
        {"wallet_address": WALLET_B, "day": "2026-07-21", "balance": 50},
    ]
    for mode in (HolderMode.daily, HolderMode.any_time):
        req = make_request(holder_mode=mode.value)
        snapshots = apply_holder_mode(parse_rows(rows, req), req)
        assert {s.wallet_address for s in snapshots} == {WALLET_A, WALLET_B}


def test_build_summary_reports_window_and_balance_envelope():
    req = make_request()
    rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 100},
        {"wallet_address": WALLET_A, "day": "2026-07-22", "balance": 300},
        {"wallet_address": WALLET_B, "day": "2026-07-21", "balance": 5},
    ]

    summary = build_summary(parse_rows(rows, req))

    by_wallet = {s.wallet_address: s for s in summary}
    a = by_wallet[WALLET_A]
    assert (a.first_date, a.last_date, a.days_held) == (
        date(2026, 7, 20),
        date(2026, 7, 22),
        2,
    )
    assert (a.min_balance, a.max_balance) == (100, 300)
    assert a.avg_balance == pytest.approx(200)
    # sorted by max_balance descending
    assert summary[0].wallet_address == WALLET_A


def test_future_end_dates_are_clamped_to_today():
    """A still-current balance row matches every future calendar day.

    Without clamping, asking for an end date in the future makes DICE project
    today's balances forward and emit snapshots for days that never happened.
    """
    from app.models import utc_today

    far_future = (utc_today() + timedelta(days=30)).isoformat()
    req = make_request(start_date=utc_today().isoformat(), end_date=far_future)

    assert req.effective_end_date == utc_today()
    assert req.end_date_clamped is True
    assert req.days == 1
    assert req.requested_days == 31


def test_a_past_end_date_is_left_alone():
    req = make_request(start_date="2026-07-20", end_date="2026-07-22")

    assert req.effective_end_date == date(2026, 7, 22)
    assert req.end_date_clamped is False
    assert req.days == 3


def test_rows_after_today_are_dropped_even_if_dune_returns_them():
    from app.models import utc_today

    today = utc_today()
    req = make_request(
        start_date=today.isoformat(),
        end_date=(today + timedelta(days=5)).isoformat(),
    )
    rows = [
        {"wallet_address": WALLET_A, "day": today.isoformat(), "balance": 10},
        {
            "wallet_address": WALLET_A,
            "day": (today + timedelta(days=3)).isoformat(),
            "balance": 10,
        },
    ]

    snapshots = parse_rows(rows, req)

    assert [s.snapshot_date for s in snapshots] == [today]


def test_start_date_in_the_future_is_rejected_with_a_clear_message():
    """An inverted range makes Dune's sequence() fail with an opaque error."""
    from app.models import utc_today
    from pydantic import ValidationError

    future = (utc_today() + timedelta(days=3)).isoformat()

    with pytest.raises(ValidationError, match="future"):
        make_request(start_date=future, end_date=future)


def test_minimum_balance_is_strict_matching_the_sql():
    """"Minimum balance: 100" means more than 100, in both filters."""
    req = make_request(min_balance=100)
    rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 100},
        {"wallet_address": WALLET_B, "day": "2026-07-20", "balance": 100.01},
    ]

    snapshots = parse_rows(rows, req)

    assert [s.wallet_address for s in snapshots] == [WALLET_B]


def test_continuous_mode_with_no_days_in_range_returns_nothing():
    """Otherwise every wallet vacuously "holds" on all zero required days."""
    from app.models import utc_today

    req = make_request(
        start_date=utc_today().isoformat(),
        end_date=utc_today().isoformat(),
        holder_mode="continuous",
    )
    object.__setattr__(req, "start_date", utc_today() + timedelta(days=1))

    assert req.days == 0
    assert apply_holder_mode([], req) == []
