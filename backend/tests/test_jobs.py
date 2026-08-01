"""The job store must be readable by a *different* process than wrote it."""

import multiprocessing
from datetime import date

from app.jobs import JobStore
from app.models import HoldersRequest, HoldersResponse, Snapshot

TOKEN = "0x1234567890abcdef1234567890abcdef12345678"
WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def make_result() -> HoldersResponse:
    req = HoldersRequest(
        chain="ethereum",
        token_address=TOKEN,
        start_date="2026-07-20",
        end_date="2026-07-21",
    )
    snapshot = Snapshot(
        wallet_address=WALLET,
        token_address=TOKEN,
        snapshot_date=date(2026, 7, 20),
        balance=1540.25,
    )
    return HoldersResponse(
        request=req,
        execution_id="exec-1",
        row_count=1,
        wallet_count=1,
        snapshots=[snapshot],
        summary=[],
    )


def test_round_trips_a_result(tmp_path):
    store = JobStore(directory=tmp_path)

    job_id = store.put(make_result())
    loaded = store.get(job_id)

    assert loaded is not None
    assert loaded.snapshots[0].wallet_address == WALLET
    assert loaded.snapshots[0].balance == 1540.25
    assert loaded.request.token_address == TOKEN


def _read_in_child(directory, job_id, queue):
    queue.put(JobStore(directory=directory).get(job_id) is not None)


def test_a_second_process_can_read_what_the_first_wrote(tmp_path):
    """uvicorn runs several workers; the reader is rarely the writer."""
    job_id = JobStore(directory=tmp_path).put(make_result())

    queue = multiprocessing.Queue()
    child = multiprocessing.Process(
        target=_read_in_child, args=(tmp_path, job_id, queue)
    )
    child.start()
    child.join(timeout=30)

    assert queue.get(timeout=5) is True


def test_unknown_and_malformed_ids_return_none(tmp_path):
    store = JobStore(directory=tmp_path)

    assert store.get("0" * 32) is None
    assert store.get("not-a-job-id") is None
    assert store.get("../../etc/passwd") is None


def test_expired_results_are_not_served(tmp_path):
    store = JobStore(directory=tmp_path, ttl_seconds=0)

    job_id = store.put(make_result())

    assert store.get(job_id) is None


def test_reading_never_evicts(tmp_path):
    """An earlier version pruned on read, so a download could delete a peer."""
    store = JobStore(directory=tmp_path, max_entries=2)
    first = store.put(make_result())
    store.put(make_result())

    for _ in range(5):
        assert store.get(first) is not None


def test_writing_past_the_cap_drops_the_oldest(tmp_path):
    store = JobStore(directory=tmp_path, max_entries=2)

    oldest = store.put(make_result())
    for _ in range(3):
        store.put(make_result())

    assert store.get(oldest) is None


def test_disk_cache_is_shared_and_expires(tmp_path):
    from app.cache import DiskCache

    cache = DiskCache(directory=tmp_path)
    cache.put("source.ethereum", {"table": "x"})

    # a second instance — i.e. another worker — sees it
    assert DiskCache(directory=tmp_path).get("source.ethereum") == {"table": "x"}

    cache.drop("source.ethereum")
    assert cache.get("source.ethereum") is None

    expired = DiskCache(directory=tmp_path, ttl_seconds=0)
    expired.put("source.base", {"table": "y"})
    assert expired.get("source.base") is None


def test_disk_cache_rejects_keys_that_could_escape_the_directory(tmp_path):
    from app.cache import DiskCache

    cache = DiskCache(directory=tmp_path)
    cache.put("../../etc/passwd", {"bad": True})

    assert cache.get("../../etc/passwd") is None
    assert not (tmp_path.parent.parent / "etc").exists()
