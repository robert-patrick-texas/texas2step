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


def test_summaries_carry_the_local_address_and_mac(tmp_path):
    store = ResultStore(tmp_path / "r.db")
    local = ("10.0.0.10", "00:11:22:33:44:55", "nic")
    client = {"ip": "203.0.113.7", "local_ip": local[0], "mac": local[1], "mac_source": local[2]}
    saved = store.add({"version": 1, "client": client}, "203.0.113.7")
    plain = store.add({"version": 1, "client": {"ip": "203.0.113.8"}}, "203.0.113.8")
    odd = store.add({"version": 1, "client": {"local_ip": 10, "mac": ["x"]}}, None)
    listed = {r["id"]: r for r in store.recent(10)}

    def fields(summary):
        return summary["client_local_ip"], summary["client_mac"], summary["client_mac_source"]

    assert fields(saved) == fields(listed[saved["id"]]) == local
    # Only text is taken from a result; anything else reads as missing.
    for summary in (plain, odd, listed[plain["id"]], listed[odd["id"]]):
        assert fields(summary) == (None, None, None)
    store.close()


def test_clear_leaves_nothing_on_disk(tmp_path):
    path = tmp_path / "r.db"
    service = ResultStore(path)  # the running service, which keeps its connection open
    for n in range(50):
        if n == 25:  # the first half in the file itself, the rest still in its WAL
            service.close()
            service = ResultStore(path)
        client = {"ip": "198.51.100.77", "user_agent": f"visitor-{n:02d}-browser"}
        service.add({"version": 1, "server": {"id": "dallas"}, "client": client}, client["ip"])
    assert b"visitor-00" in path.read_bytes()
    assert b"visitor-49" in (tmp_path / "r.db-wal").read_bytes()
    service.add({"version": 1, "server": {"id": "austin"}}, None)
    service.add({"version": 1}, None)

    admin = ResultStore(path)  # texas2step results clear
    count, first, last, servers = admin.tally()
    assert count == 52 and 0 < first <= last
    assert servers == {"dallas": 50, None: 1, "austin": 1}
    assert admin.clear() == 52
    assert admin.tally() == (0, 0, 0, {})
    admin.close()

    for left in (path, tmp_path / "r.db-wal"):
        data = left.read_bytes() if left.exists() else b""
        assert b"visitor-" not in data and b"198.51.100.77" not in data
    # The service carries on with the emptied file.
    assert service.recent(10) == []
    saved = service.add({"version": 1}, "192.0.2.1")
    assert [r["id"] for r in service.recent(10)] == [saved["id"]]
    service.close()
