"""Cohort overlap: the maths, and the trap a raw count walks into."""

import pytest
from fastapi.testclient import TestClient

from app import cohorts, db, main
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore


def _wallets(start, count):
    return [f"0x{i:040x}" for i in range(start, start + count)]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


def _cohort(client, name, wallets, chain="ethereum"):
    created = client.post(
        "/api/watchlists",
        json={
            "name": name,
            "chain": chain,
            "wallets": wallets,
            "min_wallets": 2,
            "min_wallets_pct": 0,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


# --------------------------------------------------------------------- maths


def test_lift_says_thirty_shared_wallets_is_below_chance():
    """The trap: a big raw overlap that means nothing.

    5,000 FWA farmers and 50,000 LOS holders in a million-wallet universe
    would share 250 by chance alone. Thirty is a *negative* result, and
    reporting the count on its own would sell it as a discovery.
    """
    scored = cohorts.score_pair(
        overlap=30, size_a=5_000, size_b=50_000, universe=1_000_000
    )

    assert scored["expected"] == 250.0
    assert scored["lift"] < 1          # rarer than chance, not a finding
    assert scored["containment"] == 0.6


def test_lift_flags_a_small_overlap_that_is_genuinely_surprising():
    # 300 and 400 wallets would share ~0.12 by chance; 30 is 250x that.
    scored = cohorts.score_pair(
        overlap=30, size_a=300, size_b=400, universe=1_000_000
    )

    assert scored["lift"] > 100
    assert scored["containment"] == 10.0   # a tenth of the smaller cohort


def test_containment_reads_from_the_smaller_cohort():
    scored = cohorts.score_pair(
        overlap=50, size_a=100, size_b=10_000, universe=1_000_000
    )

    # "Half of the small cohort is inside the big one" is the useful framing;
    # "0.5% of the big one" would bury it.
    assert scored["containment"] == 50.0
    assert scored["pct_of_a"] == 50.0
    assert scored["pct_of_b"] == 0.5


def test_lift_is_withheld_rather_than_invented_when_expectation_is_tiny():
    # Two 10-wallet cohorts in a million-wallet universe expect 0.0001 in
    # common; dividing by that would produce a huge number that describes the
    # universe guess, not the cohorts.
    scored = cohorts.score_pair(
        overlap=2, size_a=10, size_b=10, universe=1_000_000
    )

    assert scored["lift"] is None


def test_universe_size_moves_lift_and_is_therefore_reported(client):
    _cohort(client, "A", _wallets(1, 200))
    _cohort(client, "B", _wallets(101, 200))   # 100 shared

    small = client.get("/api/cohorts/overlap?universe=10000").json()
    large = client.get("/api/cohorts/overlap?universe=1000000").json()

    assert small["universe"] == 10_000
    assert large["universe"] == 1_000_000
    # Same data, different assumption, different verdict — which is exactly
    # why the assumption travels with the answer.
    assert large["pairs"][0]["lift"] > small["pairs"][0]["lift"]
    assert large["pairs"][0]["overlap"] == small["pairs"][0]["overlap"] == 100


# ------------------------------------------------------------------- matrix


def test_overlapping_cohorts_are_reported_with_both_sides(client):
    a = _cohort(client, "FWA farmers", _wallets(1, 100))
    b = _cohort(client, "LOS protocol", _wallets(81, 100))   # 20 shared

    body = client.get("/api/cohorts/overlap").json()

    assert body["cohorts"] == 2
    pair = body["pairs"][0]
    assert {pair["a_id"], pair["b_id"]} == {a, b}
    assert pair["overlap"] == 20
    assert pair["a_size"] == pair["b_size"] == 100
    assert pair["containment"] == 20.0


def test_pairs_rank_by_surprise_not_by_raw_count(client):
    # A big, boring overlap between two large cohorts...
    _cohort(client, "big one", _wallets(1, 400))
    _cohort(client, "big two", _wallets(301, 400))       # 100 shared
    # ...and a small, striking one between two tiny cohorts.
    _cohort(client, "tiny one", _wallets(5000, 40))
    _cohort(client, "tiny two", _wallets(5020, 40))      # 20 shared

    pairs = client.get("/api/cohorts/overlap").json()["pairs"]

    # The 20-wallet overlap outranks the 100-wallet one: half of a 40-wallet
    # cohort landing in another is a stronger statement than a quarter of a
    # 400-wallet one, even though the raw count is five times smaller.
    assert pairs[0]["overlap"] == 20
    assert pairs[0]["containment"] == 50.0
    assert pairs[1]["overlap"] == 100
    assert pairs[1]["containment"] == 25.0


def test_cohorts_on_different_chains_are_never_compared(client):
    _cohort(client, "eth side", _wallets(1, 50))
    _cohort(client, "base side", _wallets(1, 50), chain="base")

    # Identical addresses, but a wallet on Ethereum and the same string on
    # Base are not the same actor's behaviour in the same market.
    assert client.get("/api/cohorts/overlap").json()["pairs"] == []


def test_trivial_overlaps_are_filtered(client):
    _cohort(client, "A", _wallets(1, 50))
    _cohort(client, "B", _wallets(50, 50))    # exactly one shared

    assert client.get("/api/cohorts/overlap").json()["pairs"] == []
    loosened = client.get("/api/cohorts/overlap?min_overlap=1").json()
    assert loosened["pairs"][0]["overlap"] == 1


def test_a_cohort_is_never_compared_with_itself(client):
    _cohort(client, "only one", _wallets(1, 50))

    assert client.get("/api/cohorts/overlap").json()["pairs"] == []


# ------------------------------------------------------------ shared wallets


def test_the_shared_wallets_can_be_listed(client):
    a = _cohort(client, "A", _wallets(1, 100))
    b = _cohort(client, "B", _wallets(81, 100))

    body = client.get(f"/api/cohorts/overlap/{a}/{b}").json()

    assert body["count"] == 20
    assert set(body["wallets"]) == set(_wallets(81, 20))


def test_listing_an_unknown_cohort_is_a_clean_404(client):
    a = _cohort(client, "A", _wallets(1, 10))

    assert client.get(f"/api/cohorts/overlap/{a}/4242").status_code == 404


def test_deleting_a_cohort_removes_it_from_the_matrix(client):
    a = _cohort(client, "A", _wallets(1, 100))
    _cohort(client, "B", _wallets(81, 100))
    assert client.get("/api/cohorts/overlap").json()["pairs"]

    client.delete(f"/api/watchlists/{a}")

    assert client.get("/api/cohorts/overlap").json()["pairs"] == []
