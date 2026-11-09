"""Access codes, passes, and the desk's functions kept to internal networks."""

import base64
import hashlib
import io
import logging
import re
import stat
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from texas2step import __version__, access, cli, engine
from texas2step.access import Codes, is_internal, normalize
from texas2step.api import access as access_api
from texas2step.api.meta import client_address
from texas2step.app import create_app
from texas2step.config import Settings
from texas2step.lockouts import HandBlocks, LockoutStore
from texas2step.store import ID_LETTERS, ResultStore, new_id

KEY = access.parse_key(access.new_key())
OUTSIDE = {"X-Forwarded-For": "203.0.113.9"}
INSIDE = {"X-Forwarded-For": "10.1.2.3"}
NYC = "America/New_York"
NOTE = "Contact the service desk for assistance."  # access_note, by default


def codes(interval="daily", words=None, digits=2, key=KEY, zone=NYC) -> Codes:
    site = words is not None
    return Codes(key, interval, zone, digits, words or access.drawn_words(key), site, 12)


def site_list(n=120) -> list[str]:
    return [f"place{i}" for i in range(n)]


# --- Words and keys ----------------------------------------------------------------


def test_normalize_takes_any_case_and_separator():
    assert normalize("Maple River 42") == "maple.river.42"
    assert normalize("  fort-bragg_68 ") == "fort.bragg.68"
    assert normalize("austin...42") == "austin.42"
    assert normalize("") == ""


def test_built_in_words_leave_out_the_confusing_ones():
    words = access.builtin_words()
    assert len(words) == 1284 == len(set(words))
    assert all(re.fullmatch(r"[a-z]{3,5}", w) for w in words)
    for gone in ("whole", "chili", "size", "rack", "sweat", "five", "seven", "zero", "dot"):
        assert gone not in words


def test_each_key_draws_its_own_words():
    drawn = access.drawn_words(KEY)
    assert len(drawn) == access.DRAW == len(set(drawn)) and drawn == sorted(drawn)
    assert set(drawn) <= set(access.builtin_words())
    assert access.drawn_words(KEY) == drawn  # the same key, the same draw, on every server
    other = access.drawn_words(access.parse_key(access.new_key()))
    assert len(set(drawn) & set(other)) < 300  # about 40 % in common, by chance


def test_keys(tmp_path):
    text = access.new_key()
    assert len(access.parse_key(text)) == 32
    for junk in ("", "short", text + "AA", text[:-1] + "!"):
        with pytest.raises(access.AccessError, match="not an access key"):
            access.parse_key(junk)
    path = tmp_path / "access.key"
    with pytest.raises(access.AccessError, match=r"missing; make one with: sudo texas2step"):
        access.read_key(path)
    access.write_key(path, text)
    assert access.read_key(path) == access.parse_key(text)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_site_list_is_normalized_and_checked(tmp_path):
    path = tmp_path / "words.txt"
    lines = ["# our places", "Fort Bragg", "  los-alamos ", "", *site_list(98)]
    path.write_text("\n".join(lines) + "\n")
    words = access.site_words(path)
    assert words[:2] == ["fort.bragg", "los.alamos"] and len(words) == 100

    for bad, said in (
        ("Zürich", "has letters outside ASCII"),
        ("42", "has no letters"),
        ("a" * 41, "is over 40 characters"),
        ("fort-bragg", "'fort.bragg' repeats line 2"),
    ):
        path.write_text("\n".join([*lines, bad]) + "\n")
        with pytest.raises(access.AccessError, match=re.escape(said)):
            access.site_words(path)
    path.write_text("\n".join(site_list(99)) + "\n")
    with pytest.raises(access.AccessError, match="has 99 entries; a list needs at least 100"):
        access.site_words(path)


# --- Codes -------------------------------------------------------------------------


def test_built_in_codes_are_two_words_and_digits():
    c = codes()
    now = datetime(2026, 10, 22, 15, tzinfo=UTC)
    code = c.current(now).code
    first, second, digits = code.split(".")
    assert first in c.words and second in c.words and first != second
    assert re.fullmatch(r"\d\d", digits)
    assert codes().current(now).code == code  # every server with the key agrees
    assert codes(key=access.parse_key(access.new_key())).current(now).code != code
    assert re.fullmatch(r"[a-z]+\.[a-z]+\.\d{4}", codes(digits=4).current(now).code)


def test_site_codes_are_one_entry_and_digits():
    words = ["fort.bragg", *site_list()]
    c = codes(words=words)
    entry, _, digits = c.current(datetime(2026, 10, 22, tzinfo=UTC)).code.rpartition(".")
    assert entry in words and re.fullmatch(r"\d\d", digits)


def test_periods_follow_the_organizations_time_zone():
    late = datetime(2026, 10, 22, 3, 30, tzinfo=UTC)  # 23:30 on the 21st in New York
    day = codes().period(late)
    assert day.id == "2026-10-21"
    assert day.end == datetime(2026, 10, 22, tzinfo=day.end.tzinfo)
    assert codes(zone="UTC").period(late).id == "2026-10-22"
    week = codes("weekly").period(late)
    assert (week.id, week.start.weekday()) == ("2026-W43", 0)  # Monday 2026-10-19
    month = codes("monthly").period(late)
    assert month.id == "2026-10" and month.end.month == 11
    assert codes("fixed").period(late) == access.Period("fixed", None, None)
    assert codes("fixed").current(late).code == codes("fixed").current(late + timedelta(400)).code


def test_the_previous_code_works_for_a_day():
    c = codes()
    start = c.period(datetime(2026, 10, 22, 12, tzinfo=UTC)).start
    yesterday = c.current(start - timedelta(hours=1)).code
    today = c.current(start + timedelta(hours=1))
    assert today.previous == yesterday and today.code != yesterday
    assert c.check(yesterday, start + timedelta(hours=23))
    assert not c.check(yesterday, start + timedelta(hours=25))
    # A day later, today's code is the one still working for a day.
    assert c.current(start + timedelta(hours=25)).previous == today.code
    assert c.check(today.code, start + timedelta(hours=25))
    # A weekly code: last week's works on Monday, not on Wednesday.
    w = codes("weekly")
    monday = w.period(start).start
    assert w.current(monday + timedelta(hours=12)).previous
    assert w.current(monday + timedelta(days=2)).previous is None


def test_a_shuffle_changes_the_codes_and_drops_the_previous_one():
    c = codes()
    start = c.period(datetime(2026, 10, 22, 12, tzinfo=UTC)).start
    noon = start + timedelta(hours=12)
    before = c.current(noon)
    assert c.code_for(before.period, "") == before.code  # no shuffle: the codes as ever
    at = (start + timedelta(hours=11)).timestamp()  # shuffled this morning
    shuffled = c.current(noon, "0123456789abcdef", at)
    assert shuffled.code != before.code and shuffled.previous is None
    assert c.check(shuffled.code, noon, "0123456789abcdef", at)
    assert not c.check(before.code, noon, "0123456789abcdef", at)
    assert not c.check(before.previous, noon, "0123456789abcdef", at)
    assert c.current(noon, "fedcba9876543210", at).code != shuffled.code
    # The next day, the shuffled code is yesterday's, still working for a day.
    tomorrow = c.current(noon + timedelta(days=1), "0123456789abcdef", at)
    assert tomorrow.previous == shuffled.code
    assert c.check(shuffled.code, noon + timedelta(days=1), "0123456789abcdef", at)
    # A fixed code changes too.
    fixed = codes("fixed")
    assert fixed.current(noon, "0123456789abcdef", at).code != fixed.current(noon).code
    # Servers with different shuffles give different codes: their fingerprints differ.
    assert c.fingerprint("0123456789abcdef") != c.fingerprint() == c.fingerprint("")


def test_a_typed_code_counts_with_any_case_and_separators():
    c = codes()
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    first, second, digits = c.current(now).code.split(".")
    for typed in (
        f"{first}.{second}.{digits}",
        f"{first.upper()} {second.title()} {digits}",
        f"{first}-{second}-{digits}",
        f" {first}, {second}: {digits} ",
        f"{first}.{second}.{int(digits)}",  # "7" for "07"
    ):
        assert c.check(typed, now), typed
    assert not c.check(f"{second}.{first}.{digits}", now)
    assert not c.check(f"{first}.{second}.{(int(digits) + 1) % 100:02d}", now)
    assert not c.check("", now)


def test_the_fingerprint_changes_with_anything_that_changes_codes():
    print_ = codes().fingerprint()
    assert re.fullmatch(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}", print_)
    assert codes().fingerprint() == print_
    for other in (
        codes("weekly"),
        codes(zone="UTC"),
        codes(digits=3),
        codes(words=site_list()),
        codes(key=access.parse_key(access.new_key())),
    ):
        assert other.fingerprint() != print_


PHRASES = """
[[phrase]]
name = "vip"
phrase = "Copper Lantern, Harbor Violet"

[[phrase]]
name = "field-team"
phrase = "maple orbit"
until = 2026-12-31
"""
PHRASES_LIST = access.parse_phrases(PHRASES, "f")


def test_standing_phrases_are_read_and_checked(tmp_path):
    vip, team = access.parse_phrases(PHRASES, "f")
    assert (vip.name, vip.code, vip.until) == ("vip", "copper.lantern.harbor.violet", None)
    assert team.until == date(2026, 12, 31)
    assert access.parse_phrases(access.render_phrases([vip, team]), "f") == [vip, team]
    assert access.parse_phrases("", "f") == []
    assert access.parse_phrases(access.render_phrases([]), "f") == []  # examples commented out
    assert access.read_phrases(tmp_path / "none.toml") == []  # no file: no phrases
    text = '[[phrase]]\nname = "a"\nphrase = "b"\nuntil = "2026-01-02"\n'
    assert access.parse_phrases(text, "f")[0].until == date(2026, 1, 2)

    def entry(**kw):
        return "[[phrase]]\n" + "".join(f"{k} = {v}\n" for k, v in kw.items())

    for text, said in (
        ("words = 1", "expected only \\[\\[phrase\\]\\] tables"),
        ('phrase = ["a"]', "expected only"),
        ("[[phrase]", "f: "),  # not TOML
        (entry(name='"vip"', phrase='"a"', note='"x"'), "phrase 'vip': unknown note"),
        (entry(phrase='"a"'), "phrase 1: name is"),
        (entry(name='"two words"', phrase='"a"'), "name is up to 40"),
        (entry(name='"vip"'), "phrase needs letters or digits"),
        (entry(name='"vip"', phrase='" - "'), "phrase needs letters or digits"),
        (entry(name='"vip"', phrase='"café au lait"'), "outside ASCII"),
        (entry(name='"vip"', phrase='"a"', until="2026-12-31T10:00:00"), "until is a day"),
        (entry(name='"vip"', phrase='"a"', until='"soon"'), "until is a day"),
        (entry(name='"a"', phrase='"x"') + entry(name='"A"', phrase='"y"'), "name is taken"),
        (entry(name='"a"', phrase='"x y"') + entry(name='"b"', phrase='"X-Y"'), "same phrase as"),
    ):
        with pytest.raises(access.AccessError, match=said):
            access.parse_phrases(text, "f")


def test_the_examples_are_a_pattern_but_never_phrases():
    header = access.render_phrases([])
    examples = header[header.index("# [[phrase]]") :]
    uncommented = "\n".join(line.removeprefix("#").strip() for line in examples.splitlines())
    # Anyone can read them, so as they are they're refused, in any case and spacing.
    with pytest.raises(access.AccessError, match="phrase 'future': that is a published example"):
        access.parse_phrases(uncommented, "f")
    written_in = re.findall(r'^# phrase = "(.*?)"', examples, re.MULTILINE)
    assert written_in == list(access.EXAMPLE_PHRASES[:3])
    for example in access.EXAMPLE_PHRASES:
        said = example.upper().replace(" ", "-")
        with pytest.raises(access.AccessError, match="published example; make up your own"):
            access.parse_phrases(f'[[phrase]]\nname = "x"\nphrase = "{said}"\n', "f")
    # With phrases of your own in their place, the pattern works.
    own = uncommented.replace("2026 the future is bright", "fern rocket tundra")
    own = own.replace("vip ticket to ride 1776", "lantern quiver 4410")
    own = own.replace("night shift at the help desk 1836", "violet harbor keeps copper 2291")
    future, vip, alice = access.parse_phrases(own, "f")
    assert (future.name, future.text, future.until) == (
        "future",
        "fern rocket tundra",
        date(2026, 12, 31),
    )
    assert (vip.name, vip.text, vip.until) == ("vip", "lantern quiver 4410", None)
    assert not future.support and not vip.support
    # The support phrase's example meets the rule a support phrase must.
    assert (alice.name, alice.scope, alice.support) == ("alice", "support", True)
    assert access.strong(normalize(access.EXAMPLE_PHRASES[2]))
    # Writing phrases keeps the examples, still commented out.
    written = access.render_phrases(PHRASES_LIST)
    assert written.startswith(header) and access.parse_phrases(written, "f") == PHRASES_LIST


def test_the_phrases_the_docs_write_into_a_file_never_work():
    docs = (Path(__file__).parents[1] / "docs" / "access.md").read_text()
    published = re.findall(r'^(?:# )?phrase = "(.*?)"', docs, re.MULTILINE)
    assert len(published) >= 5
    refused = {normalize(example) for example in access.EXAMPLE_PHRASES}
    assert {normalize(p) for p in published} <= refused


def test_a_standing_phrase_works_until_its_day_ends():
    c = Codes(KEY, "daily", NYC, 2, access.drawn_words(KEY), False, 12, PHRASES_LIST)
    vip, team = PHRASES_LIST
    assert c.phrase("copper lantern harbor violet") == vip
    assert c.phrase("COPPER-LANTERN-HARBOR-VIOLET") == vip
    assert c.phrase("copper lantern") is None and c.phrase("") is None
    assert c.phrase("Maple Orbit") == team
    # Through the end of 2026-12-31 in New York, which is 05:00 UTC the day after.
    assert not c.expired(team, datetime(2027, 1, 1, 4, 59, tzinfo=UTC))
    assert c.expired(team, datetime(2027, 1, 1, 5, 0, tzinfo=UTC))
    assert not c.expired(vip, datetime(2099, 1, 1, tzinfo=UTC))


def test_the_fingerprint_covers_the_phrases():
    words = access.drawn_words(KEY)
    plain = Codes(KEY, "daily", NYC, 2, words, False, 12)
    assert plain.fingerprint() == codes().fingerprint()  # as before there were phrases
    with_phrases = Codes(KEY, "daily", NYC, 2, words, False, 12, PHRASES_LIST)
    renamed = [access.Phrase("other", p.text, p.until) for p in PHRASES_LIST]
    assert with_phrases.fingerprint() != plain.fingerprint()
    assert Codes(KEY, "daily", NYC, 2, words, False, 12, renamed).fingerprint() == (
        with_phrases.fingerprint()
    )
    later = [PHRASES_LIST[0], access.Phrase("field-team", "maple orbit", date(2027, 1, 1))]
    assert Codes(KEY, "daily", NYC, 2, words, False, 12, later).fingerprint() != (
        with_phrases.fingerprint()
    )


def test_passes():
    c = codes()  # 12 h passes
    token, expires = c.issue(now=1_000_000)
    assert token.startswith("1000000.") and expires == 1_000_000 + 12 * 3600
    assert c.pass_issued(token, now=1_000_100) == 1_000_000
    assert c.pass_expires(token, now=1_000_100) == expires
    assert c.pass_expires(token, now=expires) is None  # ran out
    stamp, _, sig = token.partition(".")
    assert c.pass_expires(f"{int(stamp) + 60}.{sig}", now=1_000_100) is None  # altered
    assert codes(key=access.parse_key(access.new_key())).pass_expires(token, 1_000_100) is None
    for junk in ("", "abc", "1.2.3", f"{'9' * 20}.{sig}", f"\u00b2.{sig}"):  # "²" isn't 2
        assert c.pass_expires(junk, now=1_000_100) is None


def test_how_long_a_pass_lasts_is_the_checking_servers_setting_now():
    token, _ = codes().issue(now=1_000_000)
    two = Codes(KEY, "daily", NYC, 2, codes().words, False, 2)
    assert two.pass_expires(token, now=1_000_100) == 1_000_000 + 2 * 3600  # shortened
    assert two.pass_expires(token, now=1_000_000 + 2 * 3600) is None
    day = Codes(KEY, "daily", NYC, 2, codes().words, False, 24)
    assert day.pass_expires(token, now=1_000_000 + 13 * 3600) == 1_000_000 + 24 * 3600


def test_a_pass_stamped_ahead_and_passes_ended():
    c = codes()
    # Another server's clock may be ahead: by up to PASS_AHEAD seconds.
    ahead, _ = c.issue(now=1_000_000 + access.PASS_AHEAD)
    assert c.pass_issued(ahead, now=1_000_000) == 1_000_000 + access.PASS_AHEAD
    too_far, _ = c.issue(now=1_000_001 + access.PASS_AHEAD)
    assert c.pass_issued(too_far, now=1_000_000) is None
    # Ended: a pass issued before then, or in the same second, is no pass.
    token, _ = c.issue(now=1_000_000)
    assert c.pass_issued(token, now=1_000_100, ended=1_000_000.4) is None
    assert c.pass_issued(token, now=1_000_100, ended=999_999.6) == 1_000_000
    # A new one is stamped after the end, even by a clock behind the server that ended
    # them, so it isn't refused the moment it is given.
    later, expires = c.issue(now=1_000_050, ended=1_000_100.4)
    assert later.startswith("1000101.") and expires == 1_000_101 + 12 * 3600
    assert c.pass_issued(later, now=1_000_050, ended=1_000_100.4) == 1_000_101


def test_an_earlier_versions_pass_is_not_read_as_issued_then():
    # Earlier versions signed the expiry as "pass": once that time came, such a pass would
    # otherwise look like one just issued, good for another access_pass_hours.
    c = codes()
    expires = 1_000_000 + 12 * 3600
    mac = c._mac("pass", str(expires))[:18]
    old = f"{expires}.{base64.urlsafe_b64encode(mac).decode()}"
    assert c.pass_issued(old, now=expires) is None
    assert c.pass_issued(old, now=expires + 3600) is None


# --- Support phrases and their passes ----------------------------------------------

ALICE_SAYS = "Violet harbor keeps the copper lantern 2291"
ALICE = access.Phrase("alice", ALICE_SAYS, scope="support")
BOB_SAYS = "bob walks the long quiet river 4410"
BOB = access.Phrase("bob", BOB_SAYS, scope="support")
SUPPORT = f'\n[[phrase]]\nname = "alice"\nphrase = "{ALICE_SAYS}"\nscope = "support"\n'
SUPPORT_PHRASES = PHRASES + SUPPORT  # vip and field-team for visitors, alice for the desk


def support_codes(phrases=(*PHRASES_LIST, ALICE, BOB), hours=12) -> Codes:
    return Codes(KEY, "daily", NYC, 2, access.drawn_words(KEY), False, hours, phrases)


def test_a_phrase_is_a_visitors_or_a_support_phrase():
    vip, team, alice = access.parse_phrases(SUPPORT_PHRASES, "f")
    assert (vip.scope, team.scope, alice.scope) == ("visitor", "visitor", "support")
    assert not vip.support and not team.support and alice.support and alice == ALICE
    assert access.parse_phrases(access.render_phrases([vip, team, alice]), "f") == [
        vip,
        team,
        alice,
    ]
    assert access.render_phrases([alice]).endswith('\nscope = "support"\n')
    # "visitor" said outright is as if left out, and written as before there were scopes.
    said = access.parse_phrases('[[phrase]]\nname = "a"\nphrase = "b"\nscope = "visitor"\n', "f")
    assert said == [access.Phrase("a", "b")]
    # A file without support phrases is written as before there were any.
    assert access.render_phrases(PHRASES_LIST).removeprefix(access.PHRASES_HEADER) == (
        '\n[[phrase]]\nname = "vip"\nphrase = "Copper Lantern, Harbor Violet"\n'
        '\n[[phrase]]\nname = "field-team"\nphrase = "maple orbit"\nuntil = 2026-12-31\n'
    )
    for scope in ('"desk"', '"Support"', '""', "true", '["support"]'):
        with pytest.raises(access.AccessError, match='phrase \'a\': scope is "visitor" or "supp'):
            access.parse_phrases(f'[[phrase]]\nname = "a"\nphrase = "b"\nscope = {scope}\n', "f")


def test_a_support_phrase_must_be_hard_to_guess():
    def support(text: str) -> list[access.Phrase]:
        entry = f'[[phrase]]\nname = "alice"\nphrase = "{text}"\nscope = "support"\n'
        return access.parse_phrases(entry, "f")

    for weak in (
        "maple orbit",
        "a b c d e f g h i j",  # ten words, ten letters
        "violetharborkeepsthecopperlantern",  # one word
        "violet harbor copper",  # three words, 18 letters
        "abcde fghij klmno pqrs",  # four words, 19 letters
    ):
        with pytest.raises(
            access.AccessError,
            match="f: phrase 'alice': a support phrase needs at least 4 words and 20 letters "
            "or digits",
        ):
            support(weak)
    # Just enough, with any separators; digits count.
    assert support("abcde fghij-klmno_pqrst")[0].code == "abcde.fghij.klmno.pqrst"
    assert support("2026 1836 1776 2291 1999")[0].support
    # A visitor's phrase may be as short as before.
    assert access.parse_phrases('[[phrase]]\nname = "a"\nphrase = "b"\n', "f")[0].code == "b"
    # The published example is refused as one, even as a support phrase.
    with pytest.raises(access.AccessError, match="published example"):
        support("Night shift at the HELP desk, 1836")


def test_a_site_can_ask_less_or_more_of_a_support_phrase(tmp_path, key_file):
    def support(text: str, rule: tuple[int, int]) -> list[access.Phrase]:
        entry = f'[[phrase]]\nname = "alice"\nphrase = "{text}"\nscope = "support"\n'
        return access.parse_phrases(entry, "f", rule)

    # Down to the floor: two words, twelve letters or digits.
    assert access.SUPPORT_FLOOR == (2, 12)
    assert support("copper lanterns", (2, 12))[0].code == "copper.lanterns"
    for weak in ("maple orbits", "violetharborkeeps"):  # eleven letters; one word
        with pytest.raises(
            access.AccessError, match="needs at least 2 words and 12 letters or digits"
        ):
            support(weak, (2, 12))
    # Or more than by default: alice's is seven words.
    assert support(ALICE_SAYS, (7, 30))[0].support
    with pytest.raises(access.AccessError, match="needs at least 8 words and 30 letters"):
        support(ALICE_SAYS, (8, 30))
    # The server reads its file by its own settings: a phrase its rule refuses stops it.
    phrases = tmp_path / "access-phrases.toml"
    phrases.write_text(
        '[[phrase]]\nname = "alice"\nphrase = "copper lanterns"\nscope = "support"\n'
    )
    base = {
        "access_code": "daily",
        "access_key_file": str(key_file),
        "access_phrases_file": str(phrases),
        "access_lockouts_file": str(tmp_path / "access.db"),
    }
    with pytest.raises(access.AccessError, match="needs at least 4 words and 20 letters"):
        Codes.load(Settings(**base))
    lenient = Codes.load(Settings(**base, access_support_words=2, access_support_letters=12))
    assert lenient is not None and lenient.rule == (2, 12)
    assert lenient.phrase("Copper Lanterns") == lenient.phrases[0]
    token, _ = lenient.issue(phrase=lenient.phrases[0])
    assert lenient.pass_support(token) == lenient.phrases[0]
    with pytest.raises(access.AccessError, match="needs at least 3 words and 12 letters"):
        Codes.load(Settings(**base, access_support_words=3, access_support_letters=12))


def test_the_support_settings_keep_to_the_floor():
    assert (Settings().access_support_words, Settings().access_support_letters) == (4, 20)
    assert access.support_rule(Settings()) == access.SUPPORT_RULE
    Settings(access_support_words=2, access_support_letters=12)
    Settings(access_support_words=20, access_support_letters=100)
    for kw, said in (
        ({"access_support_words": 1}, "access_support_words: expected 2 to 20 words"),
        ({"access_support_words": 21}, "access_support_words: expected 2 to 20 words"),
        ({"access_support_letters": 11}, "access_support_letters: expected 12 to 100 letters"),
        ({"access_support_letters": 101}, "access_support_letters: expected 12 to 100 letters"),
    ):
        with pytest.raises(ValueError, match=said):
            Settings(**kw)


def test_the_fingerprint_covers_the_support_rule_only_with_support_phrases():
    words = access.drawn_words(KEY)

    def fingerprint(phrases, rule) -> str:
        return Codes(KEY, "daily", NYC, 2, words, False, 12, phrases, rule).fingerprint()

    # Servers that would take different support phrases differ.
    with_alice = [*PHRASES_LIST, ALICE]
    assert fingerprint(with_alice, (4, 20)) == fingerprint(with_alice, access.SUPPORT_RULE)
    assert fingerprint(with_alice, (4, 20)) != fingerprint(with_alice, (3, 20))
    assert fingerprint(with_alice, (4, 20)) != fingerprint(with_alice, (4, 16))
    # Without a support phrase the rule changes nothing, as before there were any.
    assert fingerprint(PHRASES_LIST, (4, 20)) == fingerprint(PHRASES_LIST, (2, 12))
    assert fingerprint([], (4, 20)) == fingerprint([], (2, 12))


def test_support_phrases_count_in_the_fingerprint_and_nothing_else_changes():
    words = access.drawn_words(KEY)

    def fingerprint(phrases) -> str:
        return Codes(KEY, "daily", NYC, 2, words, False, 12, phrases).fingerprint()

    # Phrases for visitors only: as before there were scopes.
    standing = sorted(f"{p.code}|{p.until or ''}" for p in PHRASES_LIST)
    what = ("daily", NYC, "2", "built-in", hashlib.sha256("\n".join(standing).encode()).hexdigest())
    mac = codes()._mac("fingerprint", *what, hashlib.sha256("\n".join(words).encode()).hexdigest())
    assert fingerprint(PHRASES_LIST) == f"{mac.hex()[0:4]}-{mac.hex()[4:8]}-{mac.hex()[8:12]}"
    # Made a support phrase, the same phrase changes it; renamed too, as its passes carry
    # the name. A visitor's phrase renamed doesn't.
    as_support = fingerprint([*PHRASES_LIST, ALICE])
    assert as_support != fingerprint([*PHRASES_LIST, replace(ALICE, scope="visitor")])
    assert as_support != fingerprint([*PHRASES_LIST, replace(ALICE, name="carol")])
    vip, team = PHRASES_LIST
    assert fingerprint(PHRASES_LIST) == fingerprint([replace(vip, name="vip2"), team])


def test_a_support_pass_names_its_phrase():
    c = support_codes()
    visitor, _ = c.issue(now=1_000_000)
    assert re.fullmatch(r"1000000\.[A-Za-z0-9_-]{24}", visitor)  # as before
    assert c.issue(now=1_000_000, phrase=PHRASES_LIST[0])[0] == visitor  # a visitor's phrase
    token, expires = c.issue(now=1_000_000, phrase=ALICE)
    stamp, mark, name, sig = token.split(".")
    assert (stamp, mark, name) == ("1000000", "s", "YWxpY2U") and len(sig) == 24
    assert base64.urlsafe_b64decode(name + "=") == b"alice"
    assert expires == 1_000_000 + 12 * 3600
    assert c.pass_support(token, now=1_000_100) == ALICE
    assert c.pass_issued(token, now=1_000_100) == 1_000_000
    assert c.pass_expires(token, now=1_000_100) == expires
    assert c.pass_expires(token, now=expires) is None  # ran out
    assert c.pass_support(token, now=expires) is None
    # A visitor's pass opens no more than before.
    assert c.pass_support(visitor, now=1_000_100) is None
    assert c.pass_issued(visitor, now=1_000_100) == 1_000_000
    # Another server with the key and the phrases takes it; one with another key doesn't.
    assert support_codes().pass_support(token, now=1_000_100) == ALICE
    other = Codes(access.parse_key(access.new_key()), "daily", NYC, 2, [], False, 12, [ALICE])
    assert other.pass_support(token, now=1_000_100) is None
    assert other.pass_issued(token, now=1_000_100) is None


def test_a_support_pass_cant_be_altered_or_made_from_another():
    c = support_codes()
    token, _ = c.issue(now=1_000_000, phrase=ALICE)
    stamp, _, name, sig = token.split(".")
    visitor_sig = c.issue(now=1_000_000)[0].split(".")[1]
    bob = base64.urlsafe_b64encode(b"bob").rstrip(b"=").decode()
    assert c.pass_support(f"{stamp}.s.{bob}.{c._sign(1_000_000, BOB)}", now=1_000_100) == BOB
    assert base64.urlsafe_b64decode(name[:-1] + "V=") == b"alice"  # read leniently, but...
    for forged in (
        f"{stamp}.s.{bob}.{sig}",  # another support phrase's name
        f"{stamp}.s.{name[:-1]}V.{sig}",  # ...not written as issue writes it
        f"{stamp}.s.{name}=.{sig}",
        f"{stamp}.s.{base64.urlsafe_b64encode(b'Alice').rstrip(b'=').decode()}.{sig}",
        # Signed as a support pass, but vip's is a visitor's phrase.
        f"{stamp}.s.{base64.urlsafe_b64encode(b'vip').rstrip(b'=').decode()}."
        f"{c._sign(1_000_000, PHRASES_LIST[0])}",
        f"{stamp}.x.{name}.{sig}",  # the mark
        f"{stamp}.S.{name}.{sig}",
        f"{int(stamp) + 60}.s.{name}.{sig}",  # the time
        f"{stamp}.s.{name}.{sig[:-2]}AA",  # the signature
        f"{stamp}.s.{name}.{visitor_sig}",  # a visitor's signature
        f"{stamp}.{sig}",  # a support pass's signature, as a visitor's
        f"{stamp}.s.{visitor_sig}",
        f"{stamp}.s.{name}.{sig}.x",  # parts more or fewer
        f"{stamp}.s.{name}.{sig}.",
        f".{stamp}.s.{name}.{sig}",
        f"{stamp}.s.{name}",
        f"{stamp}..{name}.{sig}",
        f"{stamp}.s..{sig}",
        f"{stamp}.s.{name}.",
        f"².s.{name}.{sig}",
        f"{stamp}.s.{'QUFB' * 15}.{sig}",  # a name too long
        token.replace(".", " "),
        "",
    ):
        assert c.pass_issued(forged, now=1_000_100) is None, forged
        assert c.pass_support(forged, now=1_000_100) is None, forged


def test_a_support_pass_ends_with_its_phrase():
    token, _ = support_codes().issue(now=1_000_000, phrase=ALICE)
    vip, team = PHRASES_LIST
    for phrases in (
        [vip, team, BOB],  # removed
        [vip, team, replace(ALICE, scope="visitor"), BOB],  # a visitor's phrase now
        [vip, team, replace(ALICE, name="Alice")],  # renamed
        # A new phrase under the name, as after a leak: the old one's passes end.
        [vip, team, replace(ALICE, text="silver comet bayou thistle 42")],
        [vip, team, replace(BOB, name="alice")],
        [],
    ):
        here = support_codes(phrases)
        assert here.pass_support(token, now=1_000_100) is None, phrases
        # No pass at all, for tests too, not only History.
        assert here.pass_issued(token, now=1_000_100) is None
        assert here.pass_expires(token, now=1_000_100) is None
    # The same phrase written another way is the same phrase.
    same = support_codes([replace(ALICE, text=ALICE_SAYS.upper().replace(" ", "-"))])
    assert same.pass_support(token, now=1_000_100) is not None
    # Expired: through the end of its until day in New York, 05:00 UTC the day after.
    last = replace(ALICE, until=date(2026, 12, 31))
    end = datetime(2027, 1, 1, 5, 0, tzinfo=UTC).timestamp()
    c = support_codes([vip, last])
    token, _ = c.issue(now=end - 3600, phrase=last)
    assert c.pass_support(token, now=end - 1) == last
    assert c.pass_support(token, now=end) is None and c.pass_issued(token, now=end) is None


def test_a_support_pass_lasts_and_ends_as_any_pass():
    c = support_codes()
    token, _ = c.issue(now=1_000_000, phrase=ALICE)
    assert c.pass_support(token, now=1_000_100, ended=1_000_000.4) is None
    assert c.pass_issued(token, now=1_000_100, ended=1_000_000.4) is None
    assert c.pass_support(token, now=1_000_100, ended=999_999.6) == ALICE
    later, expires = c.issue(now=1_000_050, ended=1_000_100.4, phrase=ALICE)
    assert later.startswith("1000101.s.") and expires == 1_000_101 + 12 * 3600
    assert c.pass_support(later, now=1_000_050, ended=1_000_100.4) == ALICE
    ahead, _ = c.issue(now=1_000_000 + access.PASS_AHEAD, phrase=ALICE)
    assert c.pass_support(ahead, now=1_000_000) == ALICE
    too_far, _ = c.issue(now=1_000_001 + access.PASS_AHEAD, phrase=ALICE)
    assert c.pass_support(too_far, now=1_000_000) is None
    # How long it lasts is the checking server's access_pass_hours.
    two = support_codes(hours=2)
    assert two.pass_expires(token, now=1_000_100) == 1_000_000 + 2 * 3600
    assert two.pass_support(token, now=1_000_000 + 2 * 3600) is None


def test_who_is_internal():
    nets = ["10.0.0.0/8", "2001:db8::/32", "198.51.100.7"]
    assert is_internal("127.0.0.1", []) and is_internal("::1", [])
    assert is_internal("10.200.1.1", nets) and is_internal("::ffff:10.0.0.1", nets)
    assert is_internal("2001:db8::5", nets) and is_internal("198.51.100.7", nets)
    assert not is_internal("198.51.100.8", nets) and not is_internal("203.0.113.9", nets)
    assert not is_internal("unknown", nets)


# --- The server --------------------------------------------------------------------


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "access.key"
    access.write_key(path, access.new_key())
    return path


def gated(tmp_path, key_file, **kw) -> TestClient:
    settings = Settings(
        results_path=str(tmp_path / "r.db"),
        trusted_proxies=["testclient"],  # so X-Forwarded-For names the visitor
        access_key_file=str(key_file),
        access_phrases_file=str(tmp_path / "access-phrases.toml"),
        access_lockouts_file=str(tmp_path / "access.db"),
        **({"access_code": "daily"} | kw),
    )
    return TestClient(create_app(settings))


def unlock(c: TestClient, headers=OUTSIDE) -> str:
    code = c.app.state.codes.current(datetime.now(UTC)).code
    r = c.post("/api/v1/access", json={"code": code.upper().replace(".", " ")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["pass"]


def test_visitors_need_a_pass_for_tests(tmp_path, key_file, caplog):
    with gated(tmp_path, key_file) as c:
        status = c.get("/api/v1/access", headers=OUTSIDE).json()
        assert status == {
            "required": True,
            "desk": True,
            "codes": True,
            "note": NOTE,
            "support": False,
        }
        for open_path in ("/", "/api/v1/health", "/favicon.svg"):
            assert c.get(open_path, headers=OUTSIDE).status_code == 200, open_path
        for path in ("/api/v1/info", "/api/v1/locations", "/api/v1/ping", "/api/v1/whoami"):
            assert c.get(path, headers=OUTSIDE).status_code == 401, path
        r = c.get("/api/v1/ping", headers=OUTSIDE | {"Origin": "https://other.example"})
        assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "*"
        assert c.post("/api/v1/up", content=b"x", headers=OUTSIDE).status_code == 401
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/v1/ws", headers=OUTSIDE):
            pass

        with caplog.at_level(logging.WARNING):
            r = c.post("/api/v1/access", json={"code": "wrong.words.00"}, headers=OUTSIDE)
        assert r.status_code == 403 and "wrong access code from 203.0.113.9" in caplog.text

        token = unlock(c)
        assert c.get("/api/v1/info", params={"pass": token}, headers=OUTSIDE).status_code == 200
        r = c.get("/api/v1/access", params={"pass": token}, headers=OUTSIDE).json()
        assert r["pass"]["valid"] and r["pass"]["expires"] > time.time()
        with c.websocket_connect(f"/api/v1/ws?pass={token}", headers=OUTSIDE) as ws:
            ws.send_bytes(b"\x01")
            assert ws.receive_bytes() == b"\x01"
        # The same key on another server: the pass works there too.
        with gated(tmp_path, key_file) as other:
            assert (
                other.get("/api/v1/ping", params={"pass": token}, headers=OUTSIDE).status_code
                == 200
            )


def test_the_code_box_says_the_sites_note(tmp_path, key_file):
    with gated(tmp_path, key_file, access_note="Call 555-0100 for today's code.") as c:
        assert (
            c.get("/api/v1/access", headers=OUTSIDE).json()["note"]
            == "Call 555-0100 for today's code."
        )
    with gated(tmp_path, key_file, access_note="") as c:
        assert c.get("/api/v1/access", headers=OUTSIDE).json()["note"] == ""


def test_a_card_only_code_screen_is_marked_for_visitors_who_need_the_code(tmp_path, key_file):
    with gated(tmp_path, key_file, access_card_only=True, internal_networks=["10.0.0.0/8"]) as c:
        assert "data-card-only" in c.get("/", headers=OUTSIDE).text
        assert "data-card-only" not in c.get("/", headers=INSIDE).text
    with gated(tmp_path, key_file) as c:  # off by default
        assert "data-card-only" not in c.get("/", headers=OUTSIDE).text
    with gated(tmp_path, key_file, access_card_only=True, access_code="off") as c:
        assert "data-card-only" not in c.get("/", headers=OUTSIDE).text


def test_loopback_and_internal_networks_need_no_code(tmp_path, key_file):
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        status = c.get("/api/v1/access", headers=INSIDE).json()
        assert status == {
            "required": False,
            "desk": True,
            "codes": True,
            "note": NOTE,
            "support": False,
        }
        assert c.get("/api/v1/info", headers=INSIDE).status_code == 200
        assert c.get("/api/v1/info", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 200
        assert c.get("/api/v1/info", headers=OUTSIDE).status_code == 401


def test_a_standing_phrase_buys_a_pass_and_is_logged(tmp_path, key_file, caplog):
    yesterday = datetime.now(ZoneInfo(NYC)).date() - timedelta(1)
    (tmp_path / "access-phrases.toml").write_text(
        PHRASES + f'\n[[phrase]]\nname = "gala"\nphrase = "spring gala"\nuntil = {yesterday}\n'
    )
    with gated(tmp_path, key_file) as c, caplog.at_level(logging.WARNING):
        r = c.post("/api/v1/access", json={"code": "Copper lantern harbor violet"}, headers=OUTSIDE)
        assert r.status_code == 200
        token = r.json()["pass"]
        assert c.get("/api/v1/info", params={"pass": token}, headers=OUTSIDE).status_code == 200
        assert "access phrase vip used from 203.0.113.9" in caplog.text
        r = c.post("/api/v1/access", json={"code": "spring gala"}, headers=OUTSIDE)
        assert r.status_code == 403 and r.json()["detail"] == "that isn't the current access code"
        assert "expired access phrase gala from 203.0.113.9" in caplog.text
        assert c.app.state.lockouts.current() == ([], 1)  # it counts as a wrong code
        unlock(c)  # the day's code still works beside the phrases
        # A phrase's pass is like the code's: the day's code stays the desk's.
        r = c.get("/api/v1/access/code", params={"pass": token}, headers=OUTSIDE)
        assert r.status_code == 404
    assert "copper" not in caplog.text.lower() and "spring" not in caplog.text


def test_a_broken_phrases_file_stops_the_server(tmp_path, key_file):
    (tmp_path / "access-phrases.toml").write_text('[[phrase]]\nname = "vip"\n')
    with pytest.raises(ValueError, match="phrase 'vip': phrase needs letters or digits"):
        gated(tmp_path, key_file)
    # While access codes are off, the file isn't read.
    gated(tmp_path, key_file, access_code="off").close()


# --- Support passes at the server ---------------------------------------------------


def support_server(tmp_path, key_file, phrases=SUPPORT_PHRASES, **kw) -> TestClient:
    """A server with internal_networks, vip and field-team for visitors, alice for the desk."""
    (tmp_path / "access-phrases.toml").write_text(phrases)
    return gated(tmp_path, key_file, **({"internal_networks": ["10.0.0.0/8"]} | kw))


def enter(c: TestClient, code: str, headers=OUTSIDE) -> dict:
    r = c.post("/api/v1/access", json={"code": code}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def expired(phrases: str) -> str:
    """The phrases, alice's ended yesterday."""
    yesterday = datetime.now(ZoneInfo(NYC)).date() - timedelta(1)
    return phrases.replace('scope = "support"', f'scope = "support"\nuntil = {yesterday}')


def test_a_support_phrase_buys_a_support_pass(tmp_path, key_file, caplog):
    with support_server(tmp_path, key_file) as c, caplog.at_level(logging.WARNING):
        got = enter(c, "VIOLET harbor-keeps the copper lantern, 2291")
        assert got["scope"] == "support" and got["pass"].split(".")[1] == "s"
        assert got["expires"] > time.time()
        assert "access phrase alice (support) used from 203.0.113.9" in caplog.text
        # The day's code and the visitors' phrases buy visitors' passes, as before.
        vip = enter(c, "copper lantern harbor violet")
        assert vip["scope"] == "visitor" and len(vip["pass"].split(".")) == 2
        assert "access phrase vip used from 203.0.113.9" in caplog.text
        code = c.app.state.codes.current(datetime.now(UTC)).code
        assert enter(c, code)["scope"] == "visitor"
        # From inside, a support pass all the same.
        assert enter(c, ALICE_SAYS, INSIDE)["scope"] == "support"
        assert "access phrase alice (support) used from 10.1.2.3" in caplog.text
        # Wrong entries count as before; part of the phrase is a wrong one.
        r = c.post("/api/v1/access", json={"code": "violet harbor keeps"}, headers=OUTSIDE)
        assert r.status_code == 403 and r.json()["detail"] == access_api.WRONG
        assert c.app.state.lockouts.current() == ([], 1)
    assert "violet" not in caplog.text.lower() and got["pass"] not in caplog.text
    caplog.clear()
    with support_server(tmp_path, key_file, expired(SUPPORT_PHRASES)) as c:
        with caplog.at_level(logging.WARNING):
            r = c.post("/api/v1/access", json={"code": ALICE_SAYS}, headers=OUTSIDE)
        assert r.status_code == 403 and r.json()["detail"] == access_api.WRONG
        assert "expired access phrase alice (support) from 203.0.113.9" in caplog.text


def test_the_access_status_says_whether_a_pass_is_a_support_pass(tmp_path, key_file, monkeypatch):
    monkeypatch.setattr(HandBlocks, "EVERY", 0)  # read the blocks for every request
    with support_server(tmp_path, key_file) as c:
        alice = enter(c, ALICE_SAYS)["pass"]
        vip = enter(c, "copper lantern harbor violet")["pass"]

        def status(token: str | None = None, headers=OUTSIDE) -> dict:
            params = {"pass": token} if token else {}
            return c.get("/api/v1/access", params=params, headers=headers).json()

        assert status()["support"] is False and "pass" not in status()
        assert status(vip)["support"] is False and status(vip)["pass"]["valid"] is True
        got = status(alice)
        assert got["support"] is True and got["pass"]["valid"] is True
        assert got["desk"] is False and got["required"] is True  # by address, as before
        bearer = {"Authorization": f"Bearer {alice}", **OUTSIDE}  # as the page sends it
        assert c.get("/api/v1/access", headers=bearer).json()["support"] is True
        assert status(alice, INSIDE)["support"] is True
        # A block set by hand on its network ends it, as any pass; elsewhere it still works.
        c.post("/api/v1/access/blocks", json={"network": "203.0.113.0/24"}, headers=INSIDE)
        assert status(alice)["support"] is False and status(alice)["pass"]["valid"] is False
        assert status(alice, {"X-Forwarded-For": "198.51.100.8"})["support"] is True
        c.delete("/api/v1/access/blocks", params={"network": "all"}, headers=INSIDE)
        assert status(alice)["support"] is True
        c.app.state.lockouts.end_passes(time.time() + 1, "from the command line")
        assert status(alice)["support"] is False and status(alice)["pass"]["valid"] is False
        assert status(alice, INSIDE)["support"] is False
    with gated(tmp_path, key_file, access_code="off") as c:
        assert c.get("/api/v1/access", params={"pass": alice}).json()["support"] is False


def test_a_support_pass_opens_history_and_nothing_more(tmp_path, key_file, monkeypatch, caplog):
    monkeypatch.setattr(HandBlocks, "EVERY", 0)
    # bob's is a support phrase on another server with the key, and not on this one.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bob_phrase = f'\n[[phrase]]\nname = "bob"\nphrase = "{BOB_SAYS}"\nscope = "support"\n'
    with support_server(elsewhere, key_file, SUPPORT_PHRASES + bob_phrase) as there:
        bob = enter(there, BOB_SAYS)["pass"]
    with support_server(tmp_path, key_file) as c:
        rid = c.post("/api/v1/results", json={"version": 1}, headers=INSIDE).json()["id"]
        passes = {
            "none": "",
            "code": unlock(c),
            "phrase": enter(c, "copper lantern harbor violet")["pass"],
            "support": enter(c, ALICE_SAYS)["pass"],
            "removed": bob,
        }
        opened = ["/api/v1/results", f"/api/v1/results/{rid}", "/api/v1/access/code"]
        internal = [  # the desk's still, whoever asks from outside
            ("GET", "/api"),
            ("GET", "/api/docs"),
            ("GET", "/api/openapi.json"),
            ("GET", "/api/v1/access/blocks"),
            ("POST", "/api/v1/access/blocks"),
            ("DELETE", "/api/v1/access/blocks?network=all"),
            ("GET", "/api/v1/clients"),
            ("GET", "/api/v1/cpu"),
            ("GET", "/api/v1/metrics"),
            ("POST", "/api/v1/measure/latency"),
            ("POST", "/api/v1/measure/full"),
            ("DELETE", "/api/v1/results"),  # only reading, not some other method
            ("DELETE", f"/api/v1/results/{rid}"),
            ("POST", "/history"),
            ("GET", f"/api/v1/results/{rid}/x"),
            ("GET", "/api/v1/results/"),
        ]

        def ask(method: str, path: str, token: str, headers=OUTSIDE) -> int:
            auth = {"Authorization": f"Bearer {token}"} if token else {}
            body = (
                {"json": {"network": "198.51.100.0/24", "target": "x"}} if method == "POST" else {}
            )
            r = c.request(method, path, headers={**headers, **auth}, **body)
            if r.status_code == 404 and headers is OUTSIDE:
                assert r.json() == {"detail": "Not Found"}, (method, path)  # as without
            return r.status_code

        with caplog.at_level(logging.WARNING):
            for who, token in passes.items():
                for path in opened:
                    want = 200 if who == "support" else 404
                    assert ask("GET", path, token) == want, (who, path)
                    assert ask("GET", path, token, INSIDE) == 200, (who, path)
                for method, path in internal:
                    assert ask(method, path, token) == 404, (who, method, path)
                # History's page and a result's: served to anyone, as /, which asks for the
                # code itself.
                for page in ("/history", f"/r/{rid}"):
                    assert ask("GET", page, token) == 200, (who, page)
        # Inside, each answers as it would without a pass.
        for method, path in internal:
            inside = {ask(method, path, token, INSIDE) for token in passes.values()}
            assert len(inside) == 1, (method, path, inside)
        assert ask("GET", "/api/v1/access/blocks", "", INSIDE) == 200
        # Each request it opens is logged by the phrase's name, without the query.
        for path in opened:
            assert f"support phrase alice: GET {path} from 203.0.113.9" in caplog.text
        assert caplog.text.count("support phrase alice:") == len(opened)
        assert passes["support"] not in caplog.text
        r = c.get(
            "/api/v1/results",
            params={"limit": 2, "pass": passes["support"]},
            headers=OUTSIDE,
        )
        assert r.status_code == 200 and "support phrase alice: GET /api/v1/results from" in (
            caplog.text
        )
        assert passes["support"] not in caplog.text and "?limit" not in caplog.text
        # It reads what the desk reads, by an ID written any way the desk's is.
        assert ask("GET", f"/api/v1/results/{rid}", passes["support"]) == 200
        for written in (rid.upper(), rid.replace("-", ""), *(rid.replace("-", s) for s in " :+")):
            assert ask("GET", f"/api/v1/results/{written}", passes["support"]) == 200, written
            assert f"support phrase alice: GET /api/v1/results/{written} from" in caplog.text
        # Not one that would break the journal's line, nor one too long.
        for odd in (rid.replace("-", "%0A"), rid.replace("-", "%09"), "%C3%A9" + rid, "x" * 65):
            assert ask("GET", f"/api/v1/results/{odd}", passes["support"]) == 404, odd
        assert all("\n" not in r.getMessage() for r in caplog.records)
        code = c.get("/api/v1/access/code", params={"pass": passes["support"]}, headers=OUTSIDE)
        assert code.json() == c.get("/api/v1/access/code", headers=INSIDE).json()
        # From another site, as for the desk's own browsers: refused.
        other = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", **OUTSIDE}
        r = c.get("/api/v1/results", params={"pass": passes["support"]}, headers=other)
        assert r.status_code == 403 and "cross-site" in r.json()["detail"]
        link = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", **OUTSIDE}
        assert c.get("/history", headers=link).status_code == 200
        # Behind nginx at a path, the same.
        prefix = {"X-Forwarded-Prefix": "/speedtest", **OUTSIDE}
        r = c.get("/speedtest/api/v1/results", params={"pass": passes["support"]}, headers=prefix)
        assert r.status_code == 200
        r = c.get("/speedtest/api/v1/results", params={"pass": passes["code"]}, headers=prefix)
        assert r.status_code == 404
        # A block set by hand on its network ends it, as any pass, and so does ending them.
        c.post("/api/v1/access/blocks", json={"network": "203.0.113.0/24"}, headers=INSIDE)
        for path in opened:
            assert ask("GET", path, passes["support"]) == 404, path
        assert ask("GET", "/history", "") == 200  # the page needs no pass
        c.delete("/api/v1/access/blocks", params={"network": "all"}, headers=INSIDE)
        assert ask("GET", "/api/v1/results", passes["support"]) == 200
        c.app.state.lockouts.end_passes(time.time() + 1, "from the command line")
        for path in opened:
            assert ask("GET", path, passes["support"]) == 404, path
        assert ask("GET", "/api/v1/info", passes["support"]) == 401  # no pass at all


def test_a_support_pass_ends_with_its_phrase_on_each_server(tmp_path, key_file):
    with support_server(tmp_path, key_file) as c:
        alice = enter(c, ALICE_SAYS)["pass"]
        rid = c.post("/api/v1/results", json={"version": 1}, headers=INSIDE).json()["id"]
    auth = {"Authorization": f"Bearer {alice}", **OUTSIDE}
    # Restarted with alice's phrase removed, made a visitor's, or expired: no pass, for
    # tests too.
    for phrases in (
        PHRASES,
        SUPPORT_PHRASES.replace('scope = "support"', 'scope = "visitor"'),
        expired(SUPPORT_PHRASES),
    ):
        with support_server(tmp_path, key_file, phrases) as c:
            assert c.get("/api/v1/results", headers=auth).status_code == 404
            assert c.get(f"/api/v1/results/{rid}", headers=auth).status_code == 404
            assert c.get("/api/v1/access/code", headers=auth).status_code == 404
            assert c.get("/api/v1/info", headers=auth).status_code == 401
            r = c.get("/api/v1/access", headers=auth).json()
            assert r["support"] is False and r["pass"]["valid"] is False
            # History's page is still served, as with a support phrase, so the answer
            # doesn't tell whether there is one; its data isn't.
            assert c.get("/history", headers=OUTSIDE).status_code == 200
            assert c.get(f"/r/{rid}", headers=OUTSIDE).status_code == 200
            assert c.get("/history", headers=INSIDE).status_code == 200
    # A new phrase under alice's name, as after a leak: the old one's passes end too.
    rotated = SUPPORT_PHRASES.replace(ALICE_SAYS, "silver comet bayou thistle 42")
    with support_server(tmp_path, key_file, rotated) as c:
        assert c.get("/api/v1/results", headers=auth).status_code == 404
        assert c.get("/api/v1/access/code", headers=auth).status_code == 404
        assert c.get("/api/v1/info", headers=auth).status_code == 401
        assert c.get("/api/v1/access", headers=auth).json()["support"] is False
        new = {"Authorization": f"Bearer {enter(c, 'silver comet bayou thistle 42')['pass']}"}
        assert c.get("/api/v1/results", headers={**new, **OUTSIDE}).status_code == 200


def test_without_internal_networks_a_support_pass_adds_the_days_code(tmp_path, key_file, caplog):
    with support_server(tmp_path, key_file, internal_networks=[]) as c:
        code = unlock(c)
        alice = enter(c, ALICE_SAYS)["pass"]

        def get(path: str, token: str, headers=OUTSIDE) -> int:
            return c.get(path, params={"pass": token} if token else {}, headers=headers).status_code

        # Every pass holder reads History, as before.
        for token in (code, alice):
            assert get("/api/v1/results", token) == 200
        assert get("/api/v1/results", "") == 401
        assert get("/history", "") == 200
        # The day's code: a support pass's too now, and no one else's from outside.
        assert get("/api/v1/access/code", code) == 404
        with caplog.at_level(logging.WARNING):
            r = c.get("/api/v1/access/code", params={"pass": alice}, headers=OUTSIDE)
        assert (
            r.status_code == 200
            and r.json()["code"] == c.app.state.codes.current(datetime.now(UTC)).code
        )
        assert "support phrase alice: GET /api/v1/access/code from 203.0.113.9" in caplog.text
        loopback = {"X-Forwarded-For": "127.0.0.1"}
        assert get("/api/v1/access/code", "", loopback) == 404  # as before
        assert get("/api/v1/access/code", alice, loopback) == 200
        assert c.get("/api/v1/access", params={"pass": alice}, headers=OUTSIDE).json()["support"]


def test_the_page_keeps_historys_tab_hidden_for_a_support_pass(tmp_path, key_file):
    tab = '<a href="history" data-link data-page="history">History</a>'
    hidden = '<a href="history" data-link data-page="history" hidden>History</a>'
    with support_server(tmp_path, key_file) as c:
        for path in ("/", "/history", "/r/k7q2-m9xd", "/monitor"):
            page = c.get(path, headers=OUTSIDE).text
            assert (
                '<html lang="en" data-pages="test monitor diag help" data-support-pages="history">'
                in page
            ), path
            assert hidden in page and tab not in page and 'data-page="api"' not in page
        page = c.get("/", headers=INSIDE).text
        assert "data-support-pages" not in page and "data-pages" not in page
        assert tab in page and hidden not in page and 'data-page="api"' in page
        with_support = {path: c.get(path, headers=OUTSIDE).text for path in ("/", "/history")}
    # Without a support phrase, or with none that works, the same page, so it doesn't tell
    # whether a server has one.
    for phrases in (PHRASES, expired(SUPPORT_PHRASES), ""):
        with support_server(tmp_path, key_file, phrases) as c:
            for path, page in with_support.items():
                r = c.get(path, headers=OUTSIDE)
                assert (r.status_code, r.text) == (200, page), (phrases, path)
    # Otherwise, as before.
    for kw in (
        {"page_history": False},  # no History on this site
        {"internal_networks": []},  # History is every pass holder's
        {"access_code": "off"},  # no passes, so no support phrases
    ):
        with support_server(tmp_path, key_file, SUPPORT_PHRASES, **kw) as c:
            page = c.get("/", headers=OUTSIDE).text
            assert "data-support-pages" not in page and hidden not in page, kw
            assert (tab in page) == (kw == {"internal_networks": []}), kw
            if kw == {"access_code": "off"}:  # the desk's alone, as before support phrases
                assert c.get("/history", headers=OUTSIDE).status_code == 404
                assert c.get("/r/k7q2-m9xd", headers=OUTSIDE).status_code == 404


def test_three_wrong_codes_lock_a_network_out_silently(tmp_path, key_file, caplog):
    with (
        gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c,
        caplog.at_level(logging.WARNING),
    ):
        code = c.app.state.codes.current(datetime.now(UTC)).code
        answers = []
        # Three addresses of one /24: they count together.
        for last in (9, 10, 11, 12):
            ip = {"X-Forwarded-For": f"203.0.113.{last}"}
            r = c.post("/api/v1/access", json={"code": "wrong.words.00"}, headers=ip)
            answers.append((r.status_code, r.json()))
        # Locked out: the right code gets the same answer as a wrong one, unread.
        r = c.post("/api/v1/access", json={"code": code}, headers=OUTSIDE)
        answers.append((r.status_code, r.json()))
        assert set(map(str, answers)) == {str((403, {"detail": access_api.WRONG}))}
        assert "203.0.113.0/24 locked out after 3 wrong access codes in a row" in caplog.text
        assert "access code from 203.0.113.12 ignored: 203.0.113.0/24 is locked out" in caplog.text
        assert "access code from 203.0.113.9 ignored" in caplog.text
        assert code not in caplog.text
        # Another network, and the desk, are not affected.
        unlock(c, {"X-Forwarded-For": "198.51.100.9"})
        for _ in range(5):
            r = c.post("/api/v1/access", json={"code": "no"}, headers=INSIDE)
            assert r.status_code == 403
        unlock(c, INSIDE)
        # IPv6: the whole /48.
        for host in ("2001:db8:5:1::1", "2001:db8:5:2::1", "2001:db8:5:ffff::9"):
            c.post("/api/v1/access", json={"code": "no"}, headers={"X-Forwarded-For": host})
        r = c.post(
            "/api/v1/access", json={"code": code}, headers={"X-Forwarded-For": "2001:db8:5::7"}
        )
        assert r.status_code == 403
        unlock(c, {"X-Forwarded-For": "2001:db8:6::7"})
        locked, counting = c.app.state.lockouts.current()
        assert [x.network for x in locked] == ["203.0.113.0/24", "2001:db8:5::/48"]
        assert counting == 0


def test_the_desk_blocks_networks_through_the_api(tmp_path, key_file, caplog):
    with (
        gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c,
        caplog.at_level(logging.WARNING),
    ):
        token = unlock(c)
        for headers, params in ((OUTSIDE, {}), (OUTSIDE, {"pass": token})):
            assert c.get("/api/v1/access/blocks", headers=headers, params=params).status_code == 404
            r = c.post(
                "/api/v1/access/blocks", json={"network": "198.51.100.0/24"}, headers=headers
            )
            assert r.status_code == 404
        r = c.post(
            "/api/v1/access/blocks", json={"network": "198.51.100.7", "minutes": 60}, headers=INSIDE
        )
        assert r.status_code == 200
        body = r.json()
        assert (
            body["network"] == "198.51.100.7/32"
            and body["source"] == "blocked by the API from 10.1.2.3"
        )
        assert "198.51.100.7/32 blocked until" in caplog.text
        # The blocked address can't enter the right code; its neighbour can.
        code = c.app.state.codes.current(datetime.now(UTC)).code
        r = c.post(
            "/api/v1/access", json={"code": code}, headers={"X-Forwarded-For": "198.51.100.7"}
        )
        assert r.status_code == 403
        unlock(c, {"X-Forwarded-For": "198.51.100.8"})
        listed = c.get("/api/v1/access/blocks", headers=INSIDE).json()
        assert [x["network"] for x in listed["lockouts"]] == ["198.51.100.7/32"]
        for bad, said in (
            ("10.9.0.0/16", "overlaps 10.0.0.0/8"),
            ("127.0.0.1", "overlaps 127.0.0.0/8"),
            ("0.0.0.0/0", "overlaps"),
            ("nowhere", "isn't an address or a network"),
        ):
            r = c.post("/api/v1/access/blocks", json={"network": bad}, headers=INSIDE)
            assert r.status_code == 400 and said in r.json()["detail"], bad
        r = c.delete("/api/v1/access/blocks", params={"network": "198.51.100.0/24"}, headers=INSIDE)
        assert r.json() == {"cleared": ["198.51.100.7/32"]}
        unlock(c, {"X-Forwarded-For": "198.51.100.7"})
        # The server itself counts as inside, for automation run there.
        loop = {"X-Forwarded-For": "127.0.0.1"}
        r = c.post("/api/v1/access/blocks", json={"network": "2001:db8:9::/48"}, headers=loop)
        assert r.status_code == 200
        r = c.delete("/api/v1/access/blocks", params={"network": "all"}, headers=loop)
        assert r.json() == {"cleared": ["2001:db8:9::/48"]}


def test_pages_need_no_pass_and_no_cookie(tmp_path, key_file):
    """The page asks for the code itself, so pages load without a pass; what they fetch and
    open needs one, which the page keeps in its local storage, not a cookie."""
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        for page in ("/", "/monitor", "/diag", "/help"):
            assert c.get(page, headers=OUTSIDE, follow_redirects=False).status_code == 200
        # History's page too, for a support pass to open; its data stays the desk's.
        assert c.get("/history", headers=OUTSIDE).status_code == 200
        assert c.get("/api/v1/results", headers=OUTSIDE).status_code == 404
        code = c.app.state.codes.current(datetime.now(UTC)).code
        r = c.post("/api/v1/access", json={"code": code}, headers=OUTSIDE)
        assert "set-cookie" not in r.headers
        token = r.json()["pass"]
        # The pass as ?pass= (other servers, the CLI), as a header (the page's own server),
        # or in a WebSocket's subprotocol (the page's own sockets).
        bearer = {"Authorization": f"Bearer {token}", **OUTSIDE}
        assert c.get("/api/v1/info", params={"pass": token}, headers=OUTSIDE).status_code == 200
        assert c.get("/api/v1/info", headers=bearer).status_code == 200
        assert c.get("/api/v1/info", headers=OUTSIDE).status_code == 401
        wrong = {"Authorization": "Bearer 1.x", **OUTSIDE}
        assert c.get("/api/v1/info", headers=wrong).status_code == 401
        r = c.get("/api/v1/access", headers=bearer)
        assert r.json()["pass"]["valid"] is True
        protocols = ["texas2step", f"texas2step.pass.{token}"]
        # (Fresh headers each time: the test client writes into the dict it's given.)
        with c.websocket_connect("/api/v1/ws", subprotocols=protocols, headers={**OUTSIDE}) as ws:
            assert ws.accepted_subprotocol == "texas2step"
            ws.send_text("hi")
            assert ws.receive_text() == "hi"
        with (
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect("/api/v1/ws", subprotocols=["texas2step"], headers={**OUTSIDE}),
        ):
            pass
    # Without internal_networks, the desk's pages are every pass holder's: the pages load,
    # and what they fetch needs the pass.
    with gated(tmp_path, key_file) as c:
        for page in ("/history", "/r/k7q2-m9xd", "/api/docs"):
            assert c.get(page, headers=OUTSIDE, follow_redirects=False).status_code == 200, page
        assert c.get("/api/v1/results", headers=OUTSIDE).status_code == 401
        token = unlock(c)
        headers = {"Authorization": f"Bearer {token}", **OUTSIDE}
        assert c.get("/api/v1/results", headers=headers).status_code == 200
        # The explorer's description leaves out the version, as the page does.
        assert c.get("/api/openapi.json", headers=OUTSIDE).json()["info"]["version"] == ""
        on_the_server = {"X-Forwarded-For": "127.0.0.1"}  # loopback: always inside
        loopback = c.get("/api/openapi.json", headers=on_the_server).json()["info"]["version"]
        assert loopback == __version__
    # A cookie an earlier version set is removed, at the path it was set for.
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        prefix = OUTSIDE | {"X-Forwarded-Prefix": "/texas2step", "X-Forwarded-Proto": "https"}
        c.cookies.set(access_api.PASS_COOKIE, "1.old")
        code = c.app.state.codes.current(datetime.now(UTC)).code
        r = c.post("/api/v1/access", json={"code": code}, headers=prefix)
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f'{access_api.PASS_COOKIE}="";') and "Max-Age=0" in cookie
        assert "Path=/texas2step/" in cookie and "Secure" in cookie


def test_the_version_stays_inside(tmp_path, key_file):
    meta = f'<meta name="texas2step-version" content="{__version__}" />'
    # With internal_networks, only they see it: not even a visitor who entered the code,
    # and not when codes are off.
    for code in ("daily", "off"):
        with gated(tmp_path, key_file, access_code=code, internal_networks=["10.0.0.0/8"]) as c:
            assert c.get("/api/v1/health", headers=OUTSIDE).json() == {"status": "ok"}
            assert c.get("/api/v1/health", headers=INSIDE).json()["version"] == __version__
            assert meta not in c.get("/", headers=OUTSIDE).text
            assert meta in c.get("/", headers=INSIDE).text
            params = {"pass": unlock(c)} if code == "daily" else {}
            assert meta not in c.get("/", headers=OUTSIDE).text
            assert "version" not in c.get("/api/v1/info", params=params, headers=OUTSIDE).json()
            assert c.get("/api/v1/info", headers=INSIDE).json()["version"] == __version__
    # Without them, a visitor who entered the code sees it: the page gets it from /info.
    with gated(tmp_path, key_file) as c:
        assert c.get("/api/v1/health", headers=OUTSIDE).json() == {"status": "ok"}
        assert meta not in c.get("/", headers=OUTSIDE).text
        token = unlock(c)
        info = c.get("/api/v1/info", params={"pass": token}, headers=OUTSIDE).json()
        assert info["version"] == __version__
    # Without codes either, nothing asks visitors who they are: the version is everyone's.
    with gated(tmp_path, key_file, access_code="off") as c:
        assert c.get("/api/v1/health", headers=OUTSIDE).json()["version"] == __version__
        assert c.get("/api/v1/info", headers=OUTSIDE).json()["version"] == __version__


def test_the_pass_and_the_desks_answers_are_kept_in_no_browser_cache(tmp_path, key_file):
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        code = c.app.state.codes.current(datetime.now(UTC)).code
        answers = [
            c.post("/api/v1/access", json={"code": code}, headers=OUTSIDE),
            c.get("/api/v1/access/code", headers=INSIDE),
            c.get("/api/v1/access/blocks", headers=INSIDE),
        ]
    assert [r.status_code for r in answers] == [200, 200, 200]
    assert [r.headers["cache-control"] for r in answers] == ["no-store"] * 3


def test_a_block_set_by_hand_ends_the_passes_used_from_it(tmp_path, key_file, monkeypatch):
    monkeypatch.setattr(HandBlocks, "EVERY", 0)  # read the blocks for every request
    there = {"X-Forwarded-For": "198.51.100.8"}
    guesser = {"X-Forwarded-For": "192.0.2.50"}
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        passes = {
            who: unlock(c, h)
            for who, h in (("outside", OUTSIDE), ("there", there), ("guesser", guesser))
        }

        def works(who: str, headers: dict) -> bool:
            return (
                c.get("/api/v1/info", params={"pass": passes[who]}, headers=headers).status_code
                == 200
            )

        assert works("outside", OUTSIDE)
        r = c.post("/api/v1/access/blocks", json={"network": "203.0.113.0/24"}, headers=INSIDE)
        assert r.json()["by_hand"] is True
        # Its pass is no pass now: the page drops it and asks for the code again.
        assert not works("outside", OUTSIDE)
        r = c.get("/api/v1/info", params={"pass": passes["outside"]}, headers=OUTSIDE)
        assert (r.status_code, r.json()["detail"]) == (401, "access code required")
        r = c.get("/api/v1/access", params={"pass": passes["outside"]}, headers=OUTSIDE)
        assert r.json()["pass"]["valid"] is False
        # Other networks keep theirs; one locked out by wrong codes does too.
        assert works("there", there)
        for n in range(3):
            c.post("/api/v1/access", json={"code": f"wrong.code.{n}"}, headers=guesser)
        assert (
            c.get("/api/v1/access/blocks", headers=INSIDE).json()["lockouts"][-1]["by_hand"]
            is False
        )
        assert works("guesser", guesser)
        # Once the block ends, the pass works again.
        c.delete("/api/v1/access/blocks", params={"network": "203.0.113.0/24"}, headers=INSIDE)
        assert works("outside", OUTSIDE)


def test_blocks_through_the_api_and_clears_count_at_once(tmp_path, key_file, monkeypatch):
    monkeypatch.setattr(HandBlocks, "EVERY", 3600)  # each worker keeps its list this long
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        token = unlock(c)

        def works() -> bool:
            r = c.get("/api/v1/info", params={"pass": token}, headers=OUTSIDE)
            return r.status_code == 200

        assert works()
        net = {"network": "203.0.113.0/24"}
        c.post("/api/v1/access/blocks", json=net, headers=INSIDE)
        assert not works()  # set through this worker: it reads the list again
        c.delete("/api/v1/access/blocks", params=net, headers=INSIDE)
        assert works()  # a block found is looked up again, so an ended one ends at once
        # Set elsewhere, such as from the command line: seen when the list is read again.
        c.app.state.lockouts.block("203.0.113.0/24", 30, "the command line")
        assert works()
        monkeypatch.setattr(HandBlocks, "EVERY", 0)
        assert not works()


def test_a_block_set_by_hand_ends_open_websockets(tmp_path, key_file, monkeypatch):
    monkeypatch.setattr(HandBlocks, "EVERY", 0)
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        token = unlock(c)
        with c.websocket_connect(f"/api/v1/ws?pass={token}", headers=OUTSIDE) as ws:
            ws.send_text("1")
            assert ws.receive_text() == "1"
            c.post("/api/v1/access/blocks", json={"network": "203.0.113.0/24"}, headers=INSIDE)
            ws.send_text("2")  # the next message finds the block, and the socket ends
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1008


def test_ending_every_pass(tmp_path, key_file, monkeypatch):
    monkeypatch.setattr(HandBlocks, "EVERY", 0)  # read the end for every request
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        token = unlock(c)
        before = unlock(c, {"X-Forwarded-For": "198.51.100.8"})

        def works(pass_: str) -> bool:
            r = c.get("/api/v1/info", params={"pass": pass_}, headers=OUTSIDE)
            return r.status_code == 200

        with c.websocket_connect(f"/api/v1/ws?pass={token}", headers=OUTSIDE) as ws:
            ws.send_text("1")
            assert ws.receive_text() == "1"
            # As from texas2step access passes --end, here or on another server.
            c.app.state.lockouts.end_passes(time.time() + 1, "from the command line")
            ws.send_text("2")  # the next message finds its pass ended, and the socket ends
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1008
        assert not works(token) and not works(before)
        r = c.get("/api/v1/access", params={"pass": token}, headers=OUTSIDE)
        assert r.json()["pass"]["valid"] is False
        # The code still works, and buys a pass that does.
        again = unlock(c)
        assert works(again)
        # Inside, no pass is needed, so an ended one changes nothing.
        r = c.get("/api/v1/info", params={"pass": token}, headers=INSIDE)
        assert r.status_code == 200
        with c.websocket_connect(f"/api/v1/ws?pass={token}", headers=INSIDE) as ws:
            ws.send_text("3")
            assert ws.receive_text() == "3"


def test_a_shuffle_ends_the_codes_given_before(tmp_path, key_file):
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        before = c.get("/api/v1/access/code", headers=INSIDE).json()
        assert unlock(c)  # the code works
        token = unlock(c)
        c.app.state.lockouts.shuffle_codes("0123456789abcdef", time.time(), "by hand")
        r = c.post("/api/v1/access", json={"code": before["code"]}, headers=OUTSIDE)
        assert r.status_code == 403  # at once, on every worker: read for each code
        after = c.get("/api/v1/access/code", headers=INSIDE).json()
        assert after["code"] != before["code"] and after["previous"] is None
        r = c.post("/api/v1/access", json={"code": after["code"]}, headers=OUTSIDE)
        assert r.status_code == 200
        # Passes already given keep working.
        assert c.get("/api/v1/info", params={"pass": token}, headers=OUTSIDE).status_code == 200


def test_the_shuffle_command(tmp_path, key_file, monkeypatch, capsys):
    conf = config_file(tmp_path, key_file, access_code="daily", internal_networks=["10.0.0.0/8"])

    def access_cli(*args: str) -> str:
        cli.main(["access", *args, "--config", conf])
        return capsys.readouterr().out

    code = re.search(r"Today's code: +(\S+)", access_cli("show"))[1]
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # no terminal to ask on
    with pytest.raises(SystemExit, match="add --yes to shuffle the codes"):
        access_cli("shuffle")
    with pytest.raises(SystemExit, match="--to: expected the 16 letters and digits"):
        access_cli("shuffle", "--to", "nope")
    out = access_cli("shuffle", "--yes")
    new = re.search(r"Today's code is now (\S+);", out)[1]
    assert new != code and "Passes already given keep working" in out
    shown = access_cli("show")
    assert re.search(rf"Today's code: +{re.escape(new)} ", shown)
    assert "Yesterday's" not in shown and re.search(r"\nShuffled: +\S+ \S+ \S+, by hand\n", shown)
    # Another server given the same value gives the same code.
    value = LockoutStore(tmp_path / "access.db").codes_shuffled()[0]
    other = tmp_path / "other"
    other.mkdir()
    conf = config_file(other, key_file, access_code="daily", internal_networks=["10.0.0.0/8"])
    out = access_cli("shuffle", "--to", value, "--end-passes")
    assert f"Today's code is now {new};" in out and "Every pass issued so far is ended" in out
    assert LockoutStore(other / "access.db").passes_ended()[1] == "by hand, with a shuffle"


def test_the_passes_command(tmp_path, key_file, monkeypatch, capsys):
    db = tmp_path / "access.db"
    conf = config_file(tmp_path, key_file, access_code="daily", internal_networks=["10.0.0.0/8"])

    def access_cli(*args: str) -> str:
        cli.main(["access", *args, "--config", conf])
        return capsys.readouterr().out

    assert access_cli("passes") == (
        "A pass lasts 12 h from when it is issued here (access_pass_hours).\n"
        "No passes have been ended.\n"
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # no terminal to ask on
    with pytest.raises(SystemExit, match="add --yes to end every pass"):
        access_cli("passes", "--end")
    out = access_cli("passes", "--end", "--yes")
    assert out.startswith("Every pass issued before ") and "is ended here" in out
    ended, source = LockoutStore(db).passes_ended()
    assert abs(ended - time.time()) < 5 and source == "from the command line"
    assert "Every pass issued before" in access_cli("passes")
    assert re.search(
        r"\nPasses: +12 h from when each is issued; those issued before ", (access_cli("show"))
    )


def test_a_saved_result_gets_the_version_its_page_was_not_told(tmp_path, key_file):
    with gated(tmp_path, key_file, access_code="off", internal_networks=["10.0.0.0/8"]) as c:
        own = c.app.state.settings.server_id
        mine = {"version": 1, "tool": "texas2step-web/dev", "server": {"id": own, "url": "x"}}
        other = {"version": 1, "tool": "texas2step-web/dev", "server": {"id": "elsewhere"}}
        ids = [
            c.post("/api/v1/results", json=r, headers=OUTSIDE).json()["id"] for r in (mine, other)
        ]
        saved = [c.get(f"/api/v1/results/{i}", headers=INSIDE).json() for i in ids]
    assert saved[0]["tool"] == saved[1]["tool"] == f"texas2step-web/{__version__}"
    assert saved[0]["server"]["version"] == __version__
    assert "version" not in saved[1]["server"]  # another server's: not this one's to say


def test_the_desks_functions_refuse_requests_from_other_sites(tmp_path, key_file):
    def status(c: TestClient, path: str, **headers: str) -> int:
        return c.get(path, headers={**INSIDE, **headers}).status_code

    other = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        saved = c.post("/api/v1/results", json={"version": 1}, headers=INSIDE).json()["id"]
        # A page on another site, through an employee's browser inside: refused.
        for path in ("/api/v1/results", f"/api/v1/results/{saved}", "/api/v1/access/blocks"):
            r = c.get(path, headers={**INSIDE, **other})
            assert r.status_code == 403 and "cross-site" in r.json()["detail"], path
        block = {"network": "198.51.100.7"}
        r = c.post("/api/v1/access/blocks", json=block, headers={**INSIDE, **other})
        assert r.status_code == 403
        assert c.get("/api/v1/access/blocks", headers=INSIDE).json()["lockouts"] == []
        assert status(c, "/api/v1/results", **{**other, "Sec-Fetch-Site": "same-site"}) == 403
        # Browsers without Fetch Metadata: by their Origin; "null" is a sandboxed page.
        assert status(c, "/api/v1/results", Origin="https://evil.example") == 403
        assert status(c, "/api/v1/results", Origin="null") == 403
        # The desk's own pages, scripts and monitoring, and links from elsewhere: as before.
        assert status(c, "/api/v1/results", **{"Sec-Fetch-Site": "same-origin"}) == 200
        assert status(c, "/api/v1/results", Origin="http://testserver") == 200
        assert status(c, "/api/v1/results") == 200
        link = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        assert status(c, f"/r/{saved}", **link) == 200
        assert status(c, "/history", **link) == 200
        # The rest of the API stays open to other sites, as the other servers' pages need.
        assert status(c, "/api/v1/info", **other) == 200
        # Outside, as before: not found, whoever asks.
        same = {"Sec-Fetch-Site": "same-origin", **OUTSIDE}
        assert c.get("/api/v1/results", headers=same).status_code == 404


def test_a_proxy_without_the_visitors_address_counts_as_outside(tmp_path, key_file):
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        # A hand-written nginx location that forgets X-Forwarded-For: not loopback.
        lost = {"X-Forwarded-Proto": "https"}
        assert c.get("/api/v1/access/code", headers=lost).status_code == 404
        assert c.get("/api/v1/info", headers=lost).status_code == 401
        assert c.get("/api/v1/whoami", headers=INSIDE).json()["ip"] == "10.1.2.3"

        # On the server itself, without a proxy, it's loopback as before.
        def peer(headers: dict[str, str]) -> str:
            raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
            app = SimpleNamespace(state=SimpleNamespace(settings=Settings()))  # trusts 127.0.0.1
            scope = {"type": "http", "client": ("127.0.0.1", 5000), "headers": raw, "app": app}
            return client_address(HTTPConnection(scope))[0]

        assert peer({}) == "127.0.0.1"
        assert peer({"X-Forwarded-For": "203.0.113.9"}) == "203.0.113.9"
        for header in ("X-Forwarded-Proto", "X-Forwarded-Prefix", "X-Forwarded-Host", "Forwarded"):
            assert peer({header: "x"}) == "unknown", header


def test_request_bodies_are_capped_before_they_are_read(tmp_path, key_file):
    with gated(tmp_path, key_file) as c:
        r = c.post("/api/v1/access", content=b"x" * 5000, headers=OUTSIDE)
        assert r.status_code == 413 and "up to 1024 bytes" in r.json()["detail"]

        def chunks():  # no Content-Length: read only until the cap
            for _ in range(100):
                yield b"x" * 1000

        r = c.post("/api/v1/access", content=chunks(), headers=OUTSIDE)
        assert r.status_code == 413
        r = c.post("/api/v1/access", json={"code": "wrong.words.00"}, headers=OUTSIDE)
        assert r.status_code == 403  # a normal one still gets through
        token = unlock(c)
        big = {"version": 1, "pad": "x" * (2 << 20)}
        r = c.post("/api/v1/results", params={"pass": token}, json=big, headers=OUTSIDE)
        assert r.status_code == 413
        r = c.post("/api/v1/results", params={"pass": token}, json={"version": 1}, headers=OUTSIDE)
        assert r.status_code == 201
        # The upload test takes any size.
        r = c.post("/api/v1/up", params={"pass": token}, content=b"x" * (3 << 20), headers=OUTSIDE)
        assert r.status_code == 200


def test_the_desk_reads_the_code(tmp_path, key_file):
    with gated(tmp_path, key_file, internal_networks=["10.0.0.0/8"]) as c:
        r = c.get("/api/v1/access/code", headers=INSIDE)
        body = r.json()
        assert (
            r.status_code == 200
            and body["code"] == c.app.state.codes.current(datetime.now(UTC)).code
        )
        assert body["interval"] == "daily" and body["timezone"] == NYC and body["changes_at"]
        assert c.get("/api/v1/access/code", headers=OUTSIDE).status_code == 404
    with gated(tmp_path, key_file) as c:  # without internal networks, only the CLI shows it
        assert (
            c.get("/api/v1/access/code", headers={"X-Forwarded-For": "127.0.0.1"}).status_code
            == 404
        )


def test_desk_functions_stay_on_internal_networks(tmp_path, key_file):
    with gated(tmp_path, key_file, access_code="off", internal_networks=["10.0.0.0/8"]) as c:
        result = {"version": 1, "server": {"id": "x"}, "download": {"mbps": 1.0}}
        saved = c.post("/api/v1/results", json=result, headers=OUTSIDE)
        assert saved.status_code == 201  # a visitor saves a test for the desk to read
        rid = saved.json()["id"]
        for path in (
            "/api/v1/results",
            f"/api/v1/results/{rid}",
            f"/r/{rid}",
            "/history",
            "/api",
            "/api/docs",
            "/api/openapi.json",
        ):
            assert c.get(path, headers=OUTSIDE).status_code == 404, path
            assert c.get(path, headers=INSIDE).status_code == 200, path
        r = c.post("/api/v1/measure/latency", json={"target": "x"}, headers=OUTSIDE)
        assert r.status_code == 404
        status = c.get("/api/v1/access", headers=OUTSIDE).json()
        assert status == {
            "required": False,
            "desk": False,
            "codes": False,
            "note": NOTE,
            "support": False,
        }

        page = c.get("/", headers=OUTSIDE).text
        assert 'data-pages="test monitor diag help"' in page
        assert 'data-page="history"' not in page and 'data-page="api"' not in page
        page = c.get("/", headers=INSIDE).text
        assert "data-pages=" not in page and 'data-page="history"' in page

        # Behind nginx at a path, the same.
        prefix = OUTSIDE | {"X-Forwarded-Prefix": "/speedtest"}
        assert c.get("/speedtest/api/v1/results", headers=prefix).status_code == 404
        assert (
            c.get(
                "/api/v1/results", headers=INSIDE | {"X-Forwarded-Prefix": "/speedtest"}
            ).status_code
            == 200
        )


def test_the_journal_never_shows_a_pass(tmp_path, key_file, caplog):
    with gated(tmp_path, key_file) as c, caplog.at_level(logging.WARNING, "texas2step.access"):
        c.get("/api/v1/ping", params={"r": "1", "pass": "123.secret"}, headers=OUTSIDE)
    assert "secret" not in caplog.text and "/api/v1/ping?r=1&pass=* " in caplog.text


def test_access_settings_that_cant_work(tmp_path):
    with pytest.raises(ValueError, match=r"access\.key is missing"):
        create_app(Settings(access_code="daily", access_key_file=str(tmp_path / "access.key")))
    for kw, said in (
        ({"access_code": "hourly"}, "access_code: expected one of"),
        ({"access_timezone": "Mars/Base"}, "isn't a time zone"),
        ({"access_digits": 1}, "access_digits"),
        ({"access_pass_hours": 0}, "access_pass_hours"),
        ({"internal_networks": ["10.0.0.0/33"]}, "isn't an address or a network"),
        (
            {
                "internal_networks": ["10.0.0.0/8"],
                "page_test": False,
                "page_monitor": False,
                "page_diag": False,
            },
            "keep page_test, page_monitor or page_diag shown",
        ),
    ):
        with pytest.raises(ValueError, match=said):
            Settings(**kw)


# --- Result IDs --------------------------------------------------------------------


def test_result_ids_read_well_over_the_phone(tmp_path):
    rid = new_id()
    assert re.fullmatch(f"[{ID_LETTERS}]{{4}}-[{ID_LETTERS}]{{4}}", rid)
    store = ResultStore(str(tmp_path / "r.db"), 100)
    saved = store.add({"version": 1}, "203.0.113.9")["id"]
    for said in (saved, saved.upper(), saved.replace("-", ""), saved.replace("-", " ")):
        assert store.get(said)["id"] == saved
    # An ID from before: as it was.
    store._db.execute(
        "UPDATE results SET id = 'aB3_x-9Q', payload = json_set(payload, '$.id', 'aB3_x-9Q')"
    )
    assert store.get("aB3_x-9Q")["id"] == "aB3_x-9Q" and store.get("ab3x9q") is None
    store.close()


# --- The command line --------------------------------------------------------------


def config_file(tmp_path, key_file, **kw) -> str:
    path = tmp_path / "config.toml"
    phrases = tmp_path / "access-phrases.toml"
    lines = [
        f'access_key_file = "{key_file}"',
        f'access_phrases_file = "{phrases}"',
        # Never the host's own /var/lib/texas2step/access.db.
        f'access_lockouts_file = "{tmp_path / "access.db"}"',
        *(f"{k} = {v!r}" for k, v in kw.items()),
    ]
    path.write_text("\n".join(lines).replace("'", '"') + "\n")
    return str(path)


def test_access_key_commands(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "access.key"
    conf = config_file(tmp_path, key_file)
    cli.main(["access", "key", "--init", "--config", conf])
    first = key_file.read_text().strip()
    cli.main(["access", "key", "--init", "--config", conf])  # an existing key stays
    assert key_file.read_text().strip() == first
    capsys.readouterr()
    cli.main(["access", "key", "--config", conf])
    assert capsys.readouterr().out.strip() == first

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # no terminal to ask on
    with pytest.raises(SystemExit, match="there is a key already; add --yes"):
        cli.main(["access", "key", "--new", "--config", conf])
    cli.main(["access", "key", "--new", "--yes", "--config", conf])
    assert key_file.read_text().strip() != first
    assert "Copy it to the organization's other servers" in capsys.readouterr().out

    monkeypatch.setattr(sys, "stdin", io.StringIO(first + "\n"))
    cli.main(["access", "key", "--import", "--config", conf])
    assert key_file.read_text().strip() == first
    monkeypatch.setattr(sys, "stdin", io.StringIO("not a key"))
    with pytest.raises(SystemExit, match="not an access key"):
        cli.main(["access", "key", "--import", "--config", conf])


def test_access_show_and_words(tmp_path, key_file, capsys):
    conf = config_file(tmp_path, key_file, access_code="weekly")
    cli.main(["access", "show", "--config", conf])
    out = capsys.readouterr().out
    assert "Access codes: weekly, changing at midnight on Mondays, America/New_York." in out
    code = re.search(r"This week's code: +(\S+)", out)[1]
    assert re.fullmatch(r"[a-z]+\.[a-z]+\.\d\d", code)
    assert "512 drawn from the built-in list by the key" in out
    assert re.search(r"Fingerprint: +[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}", out)
    assert "No internal_networks are set" in out
    cli.main(["access", "words", "--config", conf])
    assert capsys.readouterr().out.split() == access.drawn_words(access.read_key(key_file))

    cli.main(["access", "show", "--config", config_file(tmp_path, key_file)])
    out = capsys.readouterr().out
    assert 'Access codes are off (access_code = "off")' in out
    assert re.search(r"\nPhrases: +none\n", out)


def test_access_show_code_only(tmp_path, key_file, capsys):
    conf = config_file(tmp_path, key_file, access_code="daily", internal_networks=["10.0.0.0/8"])
    cli.main(["access", "show", "--config", conf])
    shown = re.search(r"Today's code: +(\S+)", capsys.readouterr().out)[1]
    cli.main(["access", "show", "--code-only", "--config", conf])
    assert capsys.readouterr().out == shown + "\n"  # the code alone, for a script

    # A shuffle it can't read might have changed the code: none, rather than a wrong one.
    (tmp_path / "access.db").write_bytes(b"not a database")
    with pytest.raises(SystemExit, match="so a shuffle may have changed the code"):
        cli.main(["access", "show", "--code-only", "--config", conf])
    assert capsys.readouterr().out == ""

    # Codes off: none, though the full output shows what the daily code would be.
    off = config_file(tmp_path, key_file)
    with pytest.raises(SystemExit, match='access codes are off \\(access_code = "off"\\)'):
        cli.main(["access", "show", "--code-only", "--config", off])
    assert capsys.readouterr().out == ""


def test_access_lockout_commands(tmp_path, key_file, capsys):
    db = tmp_path / "access.db"
    conf = config_file(tmp_path, key_file, access_code="daily", internal_networks=["10.0.0.0/8"])

    def access_cli(*args: str) -> str:
        cli.main(["access", *args, "--config", conf])
        return capsys.readouterr().out

    assert access_cli("lockouts") == "No networks are locked out.\n"
    assert not db.exists()  # listing doesn't make the file
    out = access_cli("block", "203.0.113.7/24", "--minutes", "90")
    assert out.startswith("203.0.113.0/24 is locked out until ")
    with pytest.raises(SystemExit, match=re.escape("10.9.0.0/16 overlaps 10.0.0.0/8")):
        access_cli("block", "10.9.0.0/16")
    with pytest.raises(SystemExit, match="'here' isn't an address or a network"):
        access_cli("block", "here")
    with pytest.raises(SystemExit, match="--minutes: expected more than 0"):
        access_cli("block", "203.0.113.0/24", "--minutes", "0")
    store = LockoutStore(db)
    for ip in ("198.51.100.1", "198.51.100.2", "198.51.100.3", "192.0.2.1"):
        store.submit(ip, lambda: (False, "wrong access code"))
    store.close()
    lines = access_cli("lockouts").splitlines()
    assert lines[0].startswith("Locked out (the right code is refused too")
    assert lines[1].split()[:4] == ["Network", "Since", "Until", "Why"]
    assert lines[2].split()[0] == "203.0.113.0/24"
    assert lines[2].endswith("blocked from the command line")
    assert lines[3].split()[0] == "198.51.100.0/24" and lines[3].endswith("3 wrong codes in a row")
    assert lines[4] == "1 more network with wrong codes in a row."
    assert access_cli("lockouts", "--clear", "198.51.100.200") == "Ended: 198.51.100.0/24.\n"
    assert access_cli("lockouts", "--clear", "198.51.100.0/24") == "No lockout overlaps that.\n"
    assert access_cli("lockouts", "--clear", "all") == "Ended: 192.0.2.0/24, 203.0.113.0/24.\n"
    assert access_cli("lockouts") == "No networks are locked out.\n"


def test_access_phrase_commands(tmp_path, key_file, monkeypatch, capsys):
    conf = config_file(tmp_path, key_file, access_code="daily")
    path = tmp_path / "access-phrases.toml"

    def phrase(*args: str) -> None:
        cli.main(["access", "phrase", *args, "--config", conf])

    phrase("list")
    assert capsys.readouterr().out == f"There is no {path}.\n"
    phrase("init")  # as the package does on install
    assert capsys.readouterr().out == f"Wrote {path}, with three examples commented out.\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    phrase("init")  # an existing file stays
    assert capsys.readouterr().out == ""
    phrase("list")
    assert capsys.readouterr().out == f"No standing phrases in {path}.\n"
    phrase("add", "vip", "copper lantern harbor violet")
    assert "| sudo texas2step access phrase import" in capsys.readouterr().out
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    monkeypatch.setattr(sys, "stdin", io.StringIO("maple orbit\n"))  # piped in, not typed
    phrase("add", "field-team", "--until", "2099-12-31")
    with pytest.raises(SystemExit, match="a phrase named VIP already; remove it first"):
        phrase("add", "VIP", "something else")
    with pytest.raises(SystemExit, match="the same phrase as 'vip'"):
        phrase("add", "other", "Copper-Lantern Harbor-Violet")
    with pytest.raises(SystemExit, match="until is a day"):
        phrase("add", "other", "x", "--until", "soon")
    # A support phrase: strong, or refused, with the file as it was.
    before = path.read_text()
    with pytest.raises(
        SystemExit,
        match=r"^texas2step: \S+: phrase 'alice': a support phrase needs at least 4 words "
        "and 20 letters or digits$",
    ):
        phrase("add", "alice", "violet harbor", "--support")
    assert path.read_text() == before
    phrase("add", "alice", ALICE_SAYS, "--support")
    monkeypatch.setattr(sys, "stdin", io.StringIO(BOB_SAYS + "\n"))
    phrase("add", "bob", "--support", "--until", "2099-12-31")
    assert [(p.name, p.scope) for p in access.read_phrases(path)][2:] == [
        ("alice", "support"),
        ("bob", "support"),
    ]
    assert path.read_text().count('scope = "support"') == 2 + 1  # and the commented example
    capsys.readouterr()

    phrase("list")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{path}:"
    assert lines[1].split() == ["vip", "copper", "lantern", "harbor", "violet"]
    assert lines[2].split() == ["field-team", "maple", "orbit", "until", "2099-12-31"]
    assert lines[3].split() == ["alice", *ALICE_SAYS.split(), "support"]
    assert lines[4].split() == ["bob", *BOB_SAYS.split(), "support,", "until", "2099-12-31"]
    cli.main(["access", "show", "--config", conf])
    assert re.search(
        r"\nPhrases: +vip, field-team \(until 2099-12-31\), alice \(support\), "
        r"bob \(support, until 2099-12-31\)\n",
        capsys.readouterr().out,
    )
    phrase("remove", "bob")
    phrase("remove", "alice")

    text = path.read_text()
    phrase("remove", "vip")
    assert [p.name for p in access.read_phrases(path)] == ["field-team"]
    with pytest.raises(SystemExit, match="there is no phrase named vip"):
        phrase("remove", "vip")
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))  # another server's file
    phrase("import")
    assert path.read_text() == text
    monkeypatch.setattr(sys, "stdin", io.StringIO('[[phrase]]\nname = "x"\n'))
    with pytest.raises(SystemExit, match="phrase 'x': phrase needs letters or digits"):
        phrase("import")
    assert path.read_text() == text


def test_the_phrase_commands_keep_to_the_sites_support_rule(tmp_path, key_file, monkeypatch):
    lenient = config_file(
        tmp_path, key_file, access_code="daily", access_support_words=2, access_support_letters=12
    )
    path = tmp_path / "access-phrases.toml"

    def phrase(*args: str, conf: str = lenient) -> None:
        cli.main(["access", "phrase", *args, "--config", conf])

    phrase("add", "carol", "copper lanterns", "--support")
    assert [(p.name, p.scope) for p in access.read_phrases(path, (2, 12))] == [("carol", "support")]
    with pytest.raises(
        SystemExit,
        match=r"^texas2step: \S+: phrase 'dave': a support phrase needs at least 2 words "
        "and 12 letters or digits$",
    ):
        phrase("add", "dave", "maple orbits", "--support")
    phrase("list")  # read by the same rule
    text = path.read_text()
    # A server that asks for the default refuses the file, by the phrase's name, when
    # listing, adding, removing or importing.
    strict = tmp_path / "strict.toml"
    strict.write_text(Path(lenient).read_text().replace("access_support_words = 2\n", ""))
    strict.write_text(strict.read_text().replace("access_support_letters = 12\n", ""))
    for args in (("list",), ("add", "erin", "a b c d e f g h i j k"), ("remove", "carol")):
        with pytest.raises(SystemExit, match="phrase 'carol': a support phrase needs at least 4"):
            phrase(*args, conf=str(strict))
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    with pytest.raises(SystemExit, match="phrase 'carol': a support phrase needs at least 4"):
        phrase("import", conf=str(strict))
    assert path.read_text() == text


@pytest.fixture
def gated_server(tmp_path, key_file):
    """A real server that asks for a code, on loopback but taking X-Forwarded-For from
    itself, so the CLI's requests (with a forged visitor address) count as outside."""
    settings = Settings(
        results_path=str(tmp_path / "r.db"),
        access_code="daily",
        access_key_file=str(key_file),
        access_phrases_file=str(tmp_path / "access-phrases.toml"),
        access_lockouts_file=str(tmp_path / "access.db"),
        internal_networks=["10.0.0.0/8"],
        trusted_proxies=[],  # loopback stays loopback: the test marks itself outside below
    )
    app = create_app(settings)
    sock_port = _port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=sock_port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{sock_port}", app
    server.should_exit = True
    thread.join(timeout=10)


def _port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_cli_trades_its_code_for_a_pass(gated_server, monkeypatch, capsys):
    url, app = gated_server
    # Loopback is internal, so for this test treat it as outside.
    monkeypatch.setattr("texas2step.app.is_internal", lambda ip, nets: False)
    monkeypatch.setattr("texas2step.api.access.is_internal", lambda ip, nets: False)
    with pytest.raises(SystemExit, match="asks for an access code: add --code"):
        cli.main(["latency", url, "--count", "2"])
    with pytest.raises(SystemExit, match="that isn't the current access code"):
        cli.main(["latency", url, "--count", "2", "--code", "wrong.code.00"])
    code = app.state.codes.current(datetime.now(UTC)).code
    token = engine.PASS.set("")
    try:
        cli.main(
            [
                "download",
                url,
                "--seconds",
                "0.5",
                "--streams",
                "1",
                "--code",
                code.replace(".", " "),
            ]
        )
        assert "Download" in capsys.readouterr().out
        assert engine.PASS.get()  # every request after the code carried the pass
    finally:
        engine.PASS.reset(token)


def test_networks_with_spaces_around_them_are_read_as_written(tmp_path, key_file):
    with gated(tmp_path, key_file, internal_networks=[" 10.0.0.0/8 "]) as c:
        r = c.post("/api/v1/access/blocks", json={"network": " 198.51.100.7 "}, headers=INSIDE)
        assert r.status_code == 200 and r.json()["network"] == "198.51.100.7/32"
        r = c.delete(
            "/api/v1/access/blocks", params={"network": " 198.51.100.0/24 "}, headers=INSIDE
        )
        assert r.json() == {"cleared": ["198.51.100.7/32"]}
