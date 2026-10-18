"""Change single settings in place: ``texas2step config set`` and ``config add-location``.

``config merge`` rebuilds the whole file from the template. These commands edit only the
lines they need, so the operator's comments and layout stay where they are:

- ``set`` replaces the line that holds a setting, commented out or not, or adds one
  before the first ``[[table]]`` when the file has no line for it;
- ``add-location`` adds a ``[[locations]]`` table after the last one, or after the
  template's commented example.

Like the merge, the edited text must give back every value it was asked to write, keep
every other value, and pass the server's own validation, before anything is written.
They rely on the template's layout of one setting per line.
"""

from __future__ import annotations

import tomllib
import typing
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, _coerce, parse_toml
from .config_merge import SETTING, TABLE, _tables, toml_value

LOCATION_FIELDS = ("id", "name", "url", "location", "url_v4", "url_v6")


@dataclass
class Edited:
    text: str
    changes: list[str] = field(default_factory=list)  # "name: old -> new", for the operator
    notes: list[str] = field(default_factory=list)  # why nothing changed, when it didn't


def parse_value(name: str, raw: str) -> Any:
    """The value for ``name`` from the command line: TOML if it parses, else a string.

    It is then read the way an environment variable would be, so ``61000`` is a number,
    ``yes`` a boolean for a boolean setting, and ``a,b`` a list for a list setting.
    """
    hints = typing.get_type_hints(Settings)
    if name not in hints:
        raise ValueError(f"unknown setting: {name}")
    if name in _tables():
        raise ValueError(f"{name} is a list of [[{name}]] tables; edit it in the file")
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    return _coerce(value, hints[name], name)


def set_values(current: str, values: dict[str, Any]) -> Edited:
    """``current`` with each setting in ``values`` written on its own line."""
    old = tomllib.loads(current)
    lines = current.splitlines()
    edited = Edited(text=current)
    for name, value in values.items():
        if name in old and old[name] == value:
            continue
        default = getattr(Settings(), name)
        before = toml_value(old[name]) if name in old else f"{toml_value(default)} (default)"
        edited.changes.append(f"{name}: {before} -> {toml_value(value)}")
        line, note = _setting_line(lines, name)
        text = f"{name} = {toml_value(value)}{note}"
        if line is None:
            at = next((i for i, x in enumerate(lines) if TABLE.match(x)), len(lines))
            lines[at:at] = [text, ""] if at < len(lines) else [text]
        else:
            lines[line] = text
    if not edited.changes:
        edited.notes.append("every setting already has that value")
        return edited
    edited.text = "\n".join(lines).rstrip("\n") + "\n"
    _check(edited.text, {**old, **values})
    return edited


def _setting_line(lines: list[str], name: str) -> tuple[int | None, str]:
    """The line holding ``name`` before the first table, preferring an active one."""
    commented: int | None = None
    note = ""
    for i, line in enumerate(lines):
        if TABLE.match(line):
            break
        m = SETTING.match(line)
        if not m or m.group(2) != name:
            continue
        if not m.group(1):
            return i, m.group(3) or ""
        if commented is None:
            commented, note = i, m.group(3) or ""
    return commented, note


def add_location(current: str, location: dict[str, str]) -> Edited:
    """``current`` with one more ``[[locations]]`` table, unless it is already listed."""
    old = tomllib.loads(current)
    entry = {k: location[k] for k in LOCATION_FIELDS if location.get(k)}
    entry["url"] = entry.get("url", "").rstrip("/")
    for name in ("id", "name", "url"):
        if not entry.get(name):
            raise ValueError(f"a location needs an {name}")
    if entry["id"] == old.get("server_id", Settings().server_id):
        raise ValueError(f"{entry['id']!r} is this server's own server_id")
    edited = Edited(text=current)
    for loc in old.get("locations", []):
        if loc.get("id") == entry["id"] or loc.get("url", "").rstrip("/") == entry["url"]:
            edited.notes.append(f"{loc.get('id')} ({loc.get('url')}) is already listed")
            return edited

    lines = current.splitlines()
    at = _location_slot(lines)
    block = ["[[locations]]", *[f"{k} = {toml_value(v)}" for k, v in entry.items()]]
    if at > 0 and lines[at - 1].strip():
        block.insert(0, "")
    if at < len(lines) and lines[at].strip():
        block.append("")
    lines[at:at] = block
    edited.text = "\n".join(lines).rstrip("\n") + "\n"
    edited.changes.append(f"locations: + {entry['id']} ({entry['url']})")
    _check(edited.text, {**old, "locations": [*old.get("locations", []), entry]})
    return edited


def _location_slot(lines: list[str]) -> int:
    """Where a new [[locations]] table goes: after the last one, or the example."""
    headers = [i for i, x in enumerate(lines) if x.strip() == "[[locations]]"]
    if headers:
        end = next(
            (i for i in range(headers[-1] + 1, len(lines)) if TABLE.match(lines[i])), len(lines)
        )
        keys = [i for i in range(headers[-1], end) if lines[i].strip() and not _comment(lines[i])]
        return keys[-1] + 1
    # Never above a top-level setting, which would then belong to the new table.
    first_table = next((i for i, x in enumerate(lines) if _active_table(x)), len(lines))
    last_setting = max(
        (i for i in range(first_table) if SETTING.match(lines[i]) and not _comment(lines[i])),
        default=-1,
    )
    example = next((i for i, x in enumerate(lines) if x.strip() == "# [[locations]]"), None)
    if example is not None and example > last_setting:
        end = example
        while end + 1 < len(lines) and _comment(lines[end + 1]):
            end += 1
        return end + 1
    return first_table


def _comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _active_table(line: str) -> bool:
    return bool(TABLE.match(line)) and not _comment(line)


def _check(text: str, values: dict[str, Any]) -> None:
    """The edited file must hold exactly these values, and the server must accept it."""
    parsed = tomllib.loads(text)
    changed = sorted(set(parsed) ^ set(values) | {k for k in values if parsed.get(k) != values[k]})
    if changed:
        raise RuntimeError(f"edit changed {', '.join(changed)}; nothing written (please report it)")
    Settings(**parse_toml(parsed, "the edited settings"))
