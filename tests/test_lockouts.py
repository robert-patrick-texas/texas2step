"""Wrong codes per network, lockouts and blocks, shared through one SQLite file."""

import re
import sqlite3
import threading

import pytest

from texas2step.lockouts import (
    SCHEMA,
    LockoutStore,
    in_blocks,
    inside_of,
    network_of,
    parse_network,
)

WRONG = (False, "wrong access code")
RIGHT = (True, "access code")


def never():
    raise AssertionError("a locked-out network's code was read")


def test_networks_of_clients():
    assert network_of("203.0.113.77") == "203.0.113.0/24"
    assert network_of("::ffff:203.0.113.77") == "203.0.113.0/24"
    assert network_of("2001:db8:5:1234::9") == "2001:db8:5::/48"
    assert network_of("unknown") == "unknown"
    assert str(parse_network(" 198.51.100.7 ")) == "198.51.100.7/32"
    assert str(parse_network("198.51.100.7/24")) == "198.51.100.0/24"
    assert str(parse_network("::ffff:198.51.100.0/120")) == "198.51.100.0/24"
    with pytest.raises(ValueError, match="isn't an address or a network"):
        parse_network("dallas")
    internal = ["10.0.0.0/8", "2001:db8:ffff::/48"]
    assert inside_of(parse_network("10.20.0.0/16"), internal) == "10.0.0.0/8"
    assert inside_of(parse_network("0.0.0.0/0"), internal) == "10.0.0.0/8"
    assert inside_of(parse_network("127.0.0.53"), internal) == "127.0.0.0/8"
    assert inside_of(parse_network("::1"), []) == "::1/128"
    assert inside_of(parse_network("203.0.113.0/24"), internal) is None


def test_three_wrong_codes_in_a_row_lock_a_network_out(tmp_path):
    store = LockoutStore(tmp_path / "a.db", after=3, minutes=30)
    t = 1_000_000.0
    for n, ip in enumerate(("203.0.113.1", "203.0.113.2", "203.0.113.3"), 1):
        out = store.submit(ip, lambda: WRONG, now=t + n)
        assert (out.right, out.locked, out.locked_now) == (False, False, n == 3)
        assert out.network == "203.0.113.0/24" and out.note == "wrong access code"
    # Locked: even the right code is refused, unread, and each try moves the end.
    out = store.submit("203.0.113.200", never, now=t + 600)
    assert out.locked and not out.right and out.until == t + 600 + 1800
    out = store.submit("203.0.113.9", never, now=t + 600 + 1799)
    assert out.locked and out.until == t + 600 + 1799 + 1800
    locked, counting = store.current(now=t + 2000)
    assert [(x.network, x.failures, x.locked_at, x.source) for x in locked] == [
        ("203.0.113.0/24", 3, t + 3, "wrong codes")
    ]
    assert counting == 0
    # Thirty quiet minutes after the last try, it starts again from nothing.
    out = store.submit("203.0.113.9", lambda: RIGHT, now=t + 600 + 1799 + 1800)
    assert out.right and store.current(now=t + 4300) == ([], 0)
    # Another network was never affected.
    assert store.submit("198.51.100.1", lambda: RIGHT, now=t + 700).right
    store.close()


def test_a_right_code_or_a_quiet_spell_resets_the_count(tmp_path):
    store = LockoutStore(tmp_path / "a.db", after=3, minutes=30)
    t = 1_000_000.0
    store.submit("203.0.113.1", lambda: WRONG, now=t)
    store.submit("203.0.113.1", lambda: WRONG, now=t + 1)
    assert store.current(now=t + 2) == ([], 1)
    assert store.submit("203.0.113.1", lambda: RIGHT, now=t + 2).right
    for n in range(2):  # two more: still not three in a row
        assert not store.submit("203.0.113.1", lambda: WRONG, now=t + 3 + n).locked_now
    # One every 10 minutes keeps the count going (the lockout follows the third)...
    assert store.submit("203.0.113.1", lambda: WRONG, now=t + 600).locked_now
    # ...but after 30 quiet minutes a network starts over.
    store.clear(None)
    store.submit("203.0.113.1", lambda: WRONG, now=t)
    store.submit("203.0.113.1", lambda: WRONG, now=t + 1)
    out = store.submit("203.0.113.1", lambda: WRONG, now=t + 1 + 1800)
    assert not out.locked_now and store.current(now=t + 1802) == ([], 1)
    store.close()


def test_blocks_by_hand(tmp_path):
    store = LockoutStore(tmp_path / "a.db", after=3, minutes=30)
    t = 1_000_000.0
    block = store.block("198.51.100.7", 60, "command line", now=t)
    assert (block.network, block.locked_at, block.expires) == ("198.51.100.7/32", t, t + 3600)
    assert store.submit("198.51.100.7", never, now=t + 10).locked
    assert store.submit("198.51.100.8", lambda: RIGHT, now=t + 10).right  # just that address
    # Sent again, it lasts at least as long again; a shorter one doesn't cut it short.
    assert store.block("198.51.100.7", 120, "API from 10.1.2.3", now=t + 60).expires == t + 7260
    assert store.block("198.51.100.7", 1, "API from 10.1.2.3", now=t + 70).expires == t + 7260
    assert store.block("198.51.100.7", 1, "x", now=t + 80).locked_at == t  # began then
    # A wide block covers every address in it, IPv6 too.
    store.block("2001:db8::/32", 30, "command line", now=t)
    assert store.submit("2001:db8:7:7::7", never, now=t + 10).locked
    assert store.submit("2001:db9::1", lambda: RIGHT, now=t + 10).right
    locked, _ = store.current(now=t + 100)
    assert [x.network for x in locked] == ["198.51.100.7/32", "2001:db8::/32"]
    # Clearing takes every lockout that overlaps what is named.
    assert store.clear("198.51.100.0/24") == ["198.51.100.7/32"]
    assert store.clear("2001:db8:7::1") == ["2001:db8::/32"]
    assert store.clear("192.0.2.0/24") == []
    assert store.current(now=t + 100) == ([], 0)
    store.close()


def test_workers_and_the_command_line_share_one_file(tmp_path):
    path = tmp_path / "a.db"
    one, two, cli = (LockoutStore(path, after=3, minutes=30) for _ in range(3))
    one.submit("203.0.113.1", lambda: WRONG)
    two.submit("203.0.113.2", lambda: WRONG)
    one.submit("203.0.113.3", lambda: WRONG)
    assert two.submit("203.0.113.4", never).locked
    assert [x.network for x in cli.current()[0]] == ["203.0.113.0/24"]
    assert cli.clear("203.0.113.4") == ["203.0.113.0/24"]
    assert one.submit("203.0.113.4", lambda: RIGHT).right
    for store in (one, two, cli):
        store.close()


def test_guesses_sent_all_at_once_cant_slip_past_the_count(tmp_path):
    path = tmp_path / "a.db"
    workers = [LockoutStore(path, after=3, minutes=30) for _ in range(2)]
    checked = []
    lock = threading.Lock()
    start = threading.Barrier(40)

    def check():
        with lock:
            checked.append(1)
        return WRONG

    def guess(n: int) -> None:
        start.wait()
        workers[n % 2].submit(f"2001:db8:5:{n:x}::1", check)

    threads = [threading.Thread(target=guess, args=(n,)) for n in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(checked) == 3  # 40 guesses at once from one /48: three were read
    for store in workers:
        store.close()


def test_only_blocks_set_by_hand_end_passes(tmp_path):
    store = LockoutStore(tmp_path / "a.db", after=3, minutes=30)
    t = 1_000_000.0
    for n in range(3):  # a lockout from wrong codes
        store.submit("203.0.113.9", lambda: (False, "wrong"), now=t + n)
    store.block("198.51.100.7", 60, "command line", now=t)
    store.block("192.0.2.0/24", 60, "shared by dallas from 198.51.100.20", now=t, by_hand=False)
    locked, _ = store.current(now=t + 10)
    assert {x.network: x.by_hand for x in locked} == {
        "203.0.113.0/24": False,
        "198.51.100.7/32": True,
        "192.0.2.0/24": False,
    }
    blocks = store.hand_blocks(now=t + 10)
    assert blocks == [("198.51.100.7/32", t + 3600)]
    assert in_blocks("198.51.100.7", blocks, now=t + 10)
    assert not in_blocks("198.51.100.7", blocks, now=t + 3601)  # it ended
    assert not in_blocks("203.0.113.9", blocks, now=t + 10)  # locked out, not blocked
    # A lockout blocked by hand becomes a block; a later shared lockout doesn't undo that.
    store.block("203.0.113.0/24", 5, "API from 10.1.2.3", now=t + 20)
    store.block(
        "203.0.113.0/24", 5, "shared by dallas from 198.51.100.20", now=t + 30, by_hand=False
    )
    assert in_blocks("203.0.113.9", store.hand_blocks(now=t + 40), now=t + 40)


def test_a_file_from_an_earlier_version_learns_which_blocks_were_by_hand(tmp_path):
    path = tmp_path / "old.db"
    # The schema before by_hand: no such column, and no comma after source.
    old = re.sub(r"\n\s*by_hand[^\n]*", "", SCHEMA).replace(
        "source    TEXT NOT NULL,", "source    TEXT NOT NULL "
    )
    assert "by_hand" not in old.split("CREATE TABLE IF NOT EXISTS shared")[0]
    db = sqlite3.connect(path)
    db.executescript(old)
    rows = [
        ("203.0.113.0/24", "wrong codes"),
        ("198.51.100.7/32", "the command line, by root"),
        ("192.0.2.0/24", "shared by dallas from 198.51.100.20"),
    ]
    for net, source in rows:
        db.execute("INSERT INTO lockouts VALUES (?, 4, '0', 'f', 0, 1.0, 9e12, ?)", (net, source))
    db.commit()
    db.close()
    store = LockoutStore(path)
    assert [net for net, _ in store.hand_blocks()] == ["198.51.100.7/32"]
    LockoutStore(path).close()  # opened again, as by another worker: nothing to do
