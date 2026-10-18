"""Bring a settings file up to date with this version's template, keeping its values.

Each release ships ``config_template.toml``: every setting, commented out at its
default, with notes. After an upgrade, ``texas2step config merge`` rebuilds the
operator's file from the new template and carries over every value they set:

- a setting this version knows replaces the template's commented default, in place;
- ``[[locations]]`` and ``[[brand_links]]`` tables are copied to the end of the file;
- a setting this version no longer knows is kept at the end, commented out;
- a setting listed in ``RENAMED`` moves to its new name.

The merged text is parsed again and must give back every value it carried, and it must
pass the same validation the server applies, before anything is written. Comments the
operator added are not carried over; the backup keeps them.

Maintainers: keep one setting per line in the template. That is all this relies on.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, parse_toml

TEMPLATE = Path(__file__).with_name("config_template.toml")

# Old name -> new name, for when a release renames a setting.
RENAMED: dict[str, str] = {}

# A template line holding one setting: "key = value", commented out or not, with an
# optional note after two or more spaces.
SETTING = re.compile(r"^(#\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*.*?(\s{2,}#.*)?$")
TABLE = re.compile(r"^#?\s*\[")
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
RETIRED = "# --- Settings this version no longer uses (kept for reference) ---"


@dataclass
class Merged:
    text: str
    kept: list[str] = field(default_factory=list)  # settings carried over
    renamed: list[str] = field(default_factory=list)  # "old -> new"
    retired: list[str] = field(default_factory=list)  # unknown to this version, commented out


def _tables() -> set[str]:
    """Settings written as [[tables]]: lists of records, such as locations."""
    hints = typing.get_type_hints(Settings)
    return {
        name
        for name, hint in hints.items()
        if typing.get_origin(hint) is list and dataclasses.is_dataclass(typing.get_args(hint)[0])
    }


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a valid TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise ValueError(f"cannot write a {type(value).__name__} setting")


def _key(name: str) -> str:
    return name if BARE_KEY.match(name) else json.dumps(name)


def merge(current: str, template: str) -> Merged:
    """The template, filled in with the settings from ``current`` (both TOML text)."""
    old = tomllib.loads(current)
    known = set(typing.get_type_hints(Settings))
    tables = _tables()
    merged = Merged(text="")

    values: dict[str, Any] = {}
    retired: dict[str, Any] = {}
    for name, value in old.items():
        new_name = RENAMED.get(name, name)
        if new_name != name and new_name in known:
            merged.renamed.append(f"{name} -> {new_name}")
        if new_name in known:
            values[new_name] = value
        else:
            retired[name] = value

    lines: list[str] = []
    placed: set[str] = set()
    in_tables = False
    for line in template.splitlines():
        in_tables = in_tables or bool(TABLE.match(line))
        m = None if in_tables else SETTING.match(line)
        name = m.group(2) if m else None
        if m and name in values and name not in tables and name not in placed:
            lines.append(f"{name} = {toml_value(values[name])}{m.group(3) or ''}")
            placed.add(name)
        else:
            lines.append(line)
    # Settings the template has no line for still belong before any [[table]].
    loose = [n for n in values if n not in tables and n not in placed]
    if loose:
        first_table = next((i for i, x in enumerate(lines) if TABLE.match(x)), len(lines))
        block = ["", "# --- Kept from your previous settings ---"]
        block += [f"{n} = {toml_value(values[n])}" for n in loose]
        lines[first_table:first_table] = [*block, ""]
    merged.kept = [n for n in values if n not in tables]

    for name in sorted(tables & set(values)):
        lines += ["", f"# --- Your {name} (kept from your previous settings) ---"]
        for entry in values[name]:
            lines += ["", f"[[{name}]]"]
            lines += [f"{_key(k)} = {toml_value(v)}" for k, v in entry.items()]
        merged.kept.append(name)

    # Retired settings stay as comments, including those an earlier merge retired.
    notes = _retired_notes(current)
    for name, value in retired.items():
        note = f"# {_key(name)} = {toml_value(value)}"
        if note not in notes:
            notes.append(note)
        merged.retired.append(name)
    if notes:
        lines += ["", RETIRED, *notes]

    merged.text = "\n".join(lines).rstrip("\n") + "\n"
    _check(merged.text, values)
    return merged


def _retired_notes(text: str) -> list[str]:
    """The comment lines under RETIRED in a file an earlier merge wrote."""
    lines = text.splitlines()
    if RETIRED not in lines:
        return []
    notes = []
    for line in lines[lines.index(RETIRED) + 1 :]:
        if not line.startswith("# "):
            break
        notes.append(line)
    return notes


def _check(text: str, values: dict[str, Any]) -> None:
    """The merged file must hold the same values, and the server must accept it."""
    parsed = tomllib.loads(text)
    lost = [name for name, value in values.items() if parsed.get(name) != value]
    if lost:
        raise RuntimeError(f"merge changed {', '.join(lost)}; nothing written (please report this)")
    Settings(**parse_toml(parsed, "the merged settings"))
