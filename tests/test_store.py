"""The SQLite result store."""

import threading

from texas2step.store import ResultStore


def test_workers_can_open_a_new_database_together(tmp_path):
    # uvicorn starts every worker at once; on a fresh install they all create the file.
    for trial in range(20):
        path = tmp_path / f"r{trial}.db"
        barrier = threading.Barrier(4)
        errors: list[Exception] = []

        def open_store(path=path, barrier=barrier, errors=errors) -> None:
            barrier.wait()
            try:
                ResultStore(path).close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors


def test_add_and_read_back(tmp_path):
    store = ResultStore(tmp_path / "r.db")
    saved = store.add({"version": 1, "download": {"mbps": 12.5}}, "192.0.2.1")
    assert store.get(saved["id"])["download"]["mbps"] == 12.5
    assert [r["id"] for r in store.recent(10)] == [saved["id"]]
    store.close()
