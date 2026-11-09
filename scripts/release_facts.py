"""Print the facts for a release's notes: what it includes, at which versions, and its lines
of code by language. Run it after make deb and make t2s, from the tree being released:

    make -s facts

The versions come from what was built, where they can. The Python packages, and the
Python the server package needs, come from dist/texas2step-<version>-1-amd64.deb. The Go
version and modules come from dist/t2s-linux-amd64's build information. The rest come
from the files that pin them.

Lines are counted in the files git tracks, leaving out what is built from them (the
page's bundle), copied in (Swagger UI), lockfiles, data and legacy/. A code line holds
something besides comments. A comment line holds only comments, in Python docstrings
too. Blank lines count as neither.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Versions -----------------------------------------------------------------------


def version(root: Path = ROOT) -> str:
    text = (root / "src" / "texas2step" / "__init__.py").read_text()
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not found:
        raise SystemExit("release_facts: no __version__ in src/texas2step/__init__.py")
    return found[1]


def canonical(name: str) -> str:
    """A package's name as PyPI compares them: lowercase, with runs of -_. as one -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def deb_packages(listing: str) -> dict[str, str]:
    """The Python packages a server package installs, from dpkg-deb -c's listing."""
    found = re.findall(r"/site-packages/([A-Za-z0-9_.]+?)-([0-9][^/]*)\.dist-info/$", listing, re.M)
    return dict(sorted((canonical(name), ver) for name, ver in found))


def deb_python(depends: str) -> str:
    """The Python a server package needs, from its Depends: python3 (>= 3.14), ..."""
    found = re.search(r"python3 \(>= ([0-9.]+)\)", depends)
    return found[1] if found else "?"


def go_build(info: str) -> tuple[str, dict[str, str]]:
    """The Go a t2s was built with, and the modules built into it, from go version -m."""
    found = re.search(r":\s*go([0-9][^\s]*)", info.splitlines()[0] if info else "")
    deps = dict(re.findall(r"^\s*dep\s+(\S+)\s+v(\S+)", info, re.M))
    return (found[1] if found else "?"), dict(sorted(deps.items()))


def lock_versions(lock: dict, names: list[str]) -> dict[str, str]:
    """The npm packages' versions as package-lock.json installs them."""
    packages = lock.get("packages", {})
    return {name: packages.get(f"node_modules/{name}", {}).get("version", "?") for name in names}


def direct_python(root: Path = ROOT) -> list[str]:
    """The packages pyproject.toml asks for itself, not those they need."""
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    return sorted(canonical(re.split(r"[\s\[<>=!~;]", dep, maxsplit=1)[0]) for dep in deps)


def direct_go(root: Path = ROOT) -> list[str]:
    """The modules t2s/go.mod requires itself, not those they need."""
    text = (root / "t2s" / "go.mod").read_text()
    blocks = re.findall(r"^require \((.*?)^\)", text, re.M | re.S)
    lines = [line for block in blocks for line in block.splitlines()]
    lines += re.findall(r"^require (\S+ \S+.*)$", text, re.M)
    return sorted(line.split()[0] for line in lines if line.strip() and "// indirect" not in line)


def pinned(root: Path = ROOT) -> dict[str, str]:
    """The versions written in the build's own files."""
    docker = (root / "Dockerfile").read_text()
    t2s_deb = (root / "packaging" / "build-t2s-deb.sh").read_text()
    lock = json.loads((root / "web" / "package-lock.json").read_text())
    web = lock_versions(lock, ["typescript", "esbuild", "swagger-ui-dist"])

    def find(pattern: str, text: str) -> str:
        found = re.search(pattern, text, re.M)
        return found[1] if found else "?"

    return {
        "ubuntu": find(r"^ARG UBUNTU=(\S+)", docker),
        "uv": find(r"^FROM ghcr\.io/astral-sh/uv:([^@\s]+)", docker),
        "nfpm": find(r"nfpm/v2/cmd/nfpm@v([0-9][^)\s]*)", t2s_deb),
        **web,
    }


def node_version() -> str:
    node = shutil.which("node")
    if not node:
        return "?"
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    return out.strip().removeprefix("v") or "?"


def run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise SystemExit(f"release_facts: {cmd[0]} is needed") from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"release_facts: {' '.join(cmd)}: {exc.stderr.strip()}") from None


@dataclass
class Versions:
    python: str
    python_packages: dict[str, str]  # in the server package, texas2step itself left out
    python_direct: list[str]
    go: str
    go_modules: dict[str, str]  # built into t2s
    go_direct: list[str]
    pinned: dict[str, str]
    node: str


def versions(dist: Path, root: Path = ROOT) -> Versions:
    v = version(root)
    deb = dist / f"texas2step-{v}-1-amd64.deb"
    t2s = dist / "t2s-linux-amd64"
    for built, how in ((deb, "make deb"), (t2s, "make t2s")):
        if not built.is_file():
            raise SystemExit(f"release_facts: {built} is missing; run {how} first")
    packages = deb_packages(run("dpkg-deb", "-c", str(deb)))
    packages.pop("texas2step", None)
    go, modules = go_build(run(shutil.which("go") or "go", "version", "-m", str(t2s)))
    return Versions(
        python=deb_python(run("dpkg-deb", "-f", str(deb), "Depends")),
        python_packages=packages,
        python_direct=direct_python(root),
        go=go,
        go_modules=modules,
        go_direct=direct_go(root),
        pinned=pinned(root),
        node=node_version(),
    )


# --- Lines of code ------------------------------------------------------------------

LEFT_OUT = ("src/texas2step/static/", "legacy/")  # built or copied in; the old version
LEFT_OUT_FILES = {
    "uv.lock",
    "web/package-lock.json",
    "t2s/go.sum",
    "src/texas2step/words.txt",
    "tests/methodology-cases.json",
}
# The order of the table: the four the notes are about, then the rest.
LANGUAGES = (
    "Python",
    "TypeScript",
    "Go",
    "Shell",
    "Make",
    "CSS",
    "HTML",
    "JavaScript",
    "YAML",
    "TOML",
    "JSON",
    "nginx",
    "systemd",
    "Dockerfile",
)
HASH = {"Shell", "Make", "Dockerfile", "YAML", "TOML", "nginx", "systemd"}
AREAS = ("product", "tests", "tooling")
TOOLING = (
    "scripts/",
    ".github/",
    "packaging/build-",
    "packaging/t2s/",
    "Makefile",
    "Dockerfile",
    "pyproject.toml",
    "web/build.mjs",
    "web/package.json",
    "web/tsconfig.json",
)


def language(path: str, first_line: str = "") -> str | None:
    """The language of a tracked file, or None for one that isn't counted as code."""
    if path.startswith(LEFT_OUT) or path in LEFT_OUT_FILES:
        return None
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    by_ext = {
        "py": "Python",
        "ts": "TypeScript",
        "go": "Go",
        "sh": "Shell",
        "css": "CSS",
        "html": "HTML",
        "js": "JavaScript",
        "mjs": "JavaScript",
        "yml": "YAML",
        "yaml": "YAML",
        "toml": "TOML",
        "json": "JSON",
        "conf": "nginx",
        "service": "systemd",
        "timer": "systemd",
        "container": "systemd",
    }
    if ext in by_ext:
        return by_ext[ext]
    if name == "Makefile":
        return "Make"
    if name == "Dockerfile":
        return "Dockerfile"
    if not ext and first_line.startswith("#!") and first_line.rstrip().endswith("sh"):
        return "Shell"
    return None


def area(path: str) -> str:
    if path.startswith("tests/") or path.endswith("_test.go"):
        return "tests"
    if path.startswith(TOOLING):
        return "tooling"
    return "product"


def is_docs(path: str) -> bool:
    return path.endswith(".md") and not path.startswith(LEFT_OUT) and path != "LICENSE.md"


def python_lines(text: str) -> tuple[int, int]:
    """Code and comment lines of Python. A string standing alone as a statement, as a
    docstring does, counts as a comment."""
    code: set[int] = set()
    comment: set[int] = set()
    statement: list[tokenize.TokenInfo] = []

    def settle() -> None:
        rows = {row for t in statement for row in range(t.start[0], t.end[0] + 1)}
        alone = statement and all(t.type == tokenize.STRING for t in statement)
        (comment if alone else code).update(rows)
        statement.clear()

    skip = {tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                settle()
            elif tok.type == tokenize.COMMENT:
                comment.add(tok.start[0])
            elif tok.type not in skip:
                statement.append(tok)
    except (tokenize.TokenError, SyntaxError):
        return hash_lines(text)
    settle()
    return len(code), len(comment - code)


def c_lines(text: str, slashes: bool = True, raw_backticks: bool = False) -> tuple[int, int]:
    """Code and comment lines of a language with /* */ comments, and // ones unless
    ``slashes`` is false (CSS). Strings are skipped, so "//" in one is no comment; in Go a
    backtick string is raw, with no escapes."""
    code: set[int] = set()
    comment: set[int] = set()
    row, i, n = 1, 0, len(text)
    state = ""  # "", "line", "block", or the quote a string began with
    while i < n:
        ch, nxt = text[i], text[i + 1 : i + 2]
        if ch == "\n":
            row += 1
            if state in ("line", "'", '"'):  # neither kind of string goes past its line
                state = ""
        elif state == "line":
            comment.add(row)
        elif state == "block":
            comment.add(row)
            if ch == "*" and nxt == "/":
                state, i = "", i + 1
        elif state:  # in a string
            code.add(row)
            if ch == "\\" and not (state == "`" and raw_backticks):
                i += 1
            elif ch == state:
                state = ""
        elif ch == "/" and nxt in (("/", "*") if slashes else ("*",)):
            state, i = ("line" if nxt == "/" else "block"), i + 1
            comment.add(row)
        elif ch in "'\"`":
            state = ch
            code.add(row)
        elif not ch.isspace():
            code.add(row)
        i += 1
    return len(code), len(comment - code)


def hash_lines(text: str) -> tuple[int, int]:
    """Code and comment lines where a comment is a whole line that starts with #."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    comments = sum(line.startswith("#") for line in lines)
    return len(lines) - comments, comments


def html_lines(text: str) -> tuple[int, int]:
    code: set[int] = set()
    comment: set[int] = set()
    inside = False
    for row, line in enumerate(text.splitlines(), 1):
        rest = line
        while rest:
            if inside:
                end = rest.find("-->")
                comment.add(row)
                if end < 0:
                    break
                inside, rest = False, rest[end + 3 :]
            else:
                start = rest.find("<!--")
                if rest[: start if start >= 0 else len(rest)].strip():
                    code.add(row)
                if start < 0:
                    break
                inside, rest = True, rest[start + 4 :]
    return len(code), len(comment - code)


def count(lang: str, text: str) -> tuple[int, int]:
    """A file's code and comment lines."""
    if lang == "Python":
        return python_lines(text)
    if lang in ("TypeScript", "JavaScript"):
        return c_lines(text)
    if lang == "Go":
        return c_lines(text, raw_backticks=True)
    if lang == "CSS":
        return c_lines(text, slashes=False)
    if lang == "HTML":
        return html_lines(text)
    if lang in HASH:
        return hash_lines(text)
    return sum(1 for line in text.splitlines() if line.strip()), 0  # JSON


@dataclass
class Row:
    files: int = 0
    code: int = 0
    comments: int = 0
    by_area: dict[str, int] = field(default_factory=lambda: dict.fromkeys(AREAS, 0))


@dataclass
class Lines:
    rows: dict[str, Row]
    docs_files: int
    docs_lines: int  # non-blank


def tracked(root: Path = ROOT) -> list[str]:
    return run("git", "-C", str(root), "ls-files").splitlines()


def lines_of_code(root: Path = ROOT, paths: list[str] | None = None) -> Lines:
    rows: dict[str, Row] = defaultdict(Row)
    docs_files = docs_lines = 0
    for path in paths if paths is not None else tracked(root):
        file = root / path
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # an image, a PDF, or gone
            continue
        if is_docs(path):
            docs_files += 1
            docs_lines += sum(1 for line in text.splitlines() if line.strip())
            continue
        lang = language(path, text.split("\n", 1)[0])
        if lang is None:
            continue
        code, comments = count(lang, text)
        row = rows[lang]
        row.files += 1
        row.code += code
        row.comments += comments
        row.by_area[area(path)] += code
    ordered = {lang: rows[lang] for lang in LANGUAGES if lang in rows}
    return Lines(ordered, docs_files, docs_lines)


# --- The notes' sections -------------------------------------------------------------


def _n(value: int) -> str:
    return f"{value:,}" if value else "-"


def _names(names: list[str], have: dict[str, str]) -> str:
    return ", ".join(f"{name} {have.get(name, '?')}" for name in names)


def render(v: Versions, lines: Lines) -> str:
    go_direct = [m for m in v.go_direct if m in v.go_modules]
    others_py = len(v.python_packages) - len([p for p in v.python_direct if p in v.python_packages])
    others_go = len(v.go_modules) - len(go_direct)
    short = {m: m.removeprefix("github.com/") for m in v.go_modules}
    go_have = {short[m]: ver for m, ver in v.go_modules.items()}
    p = v.pinned
    out = [
        "## Versions",
        "",
        "| Part | Version |",
        "|---|---|",
        f"| Python, for the server package | {v.python} |",
        f"| Its Python packages | {_names(v.python_direct, v.python_packages)}, "
        f"and the {others_py} they need |",
        f"| Go, for t2s | {v.go} |",
        f"| Its Go modules | {_names([short[m] for m in go_direct], go_have)}, "
        f"and the {others_go} they need |",
        f"| Swagger UI, in the API tab | {p['swagger-ui-dist']} |",
        f"| TypeScript and esbuild, which build the page | {p['typescript']}, {p['esbuild']} |",
        f"| Node, which runs them | {v.node} |",
        f"| The container image's base, and uv | {p['ubuntu']}, uv {p['uv']} |",
        f"| nfpm, which builds the t2s package | {p['nfpm']} |",
        "",
        "The server package installs exactly these Python packages. The Python package on PyPI "
        "takes `pyproject.toml`'s lowest versions, so pip and uv may install newer ones.",
        "",
        f"<details><summary>Every package: {len(v.python_packages)} Python, "
        f"{len(v.go_modules)} Go</summary>",
        "",
        "| Python package | Version |",
        "|---|---|",
        *(f"| {name} | {ver} |" for name, ver in v.python_packages.items()),
        "",
        "| Go module | Version |",
        "|---|---|",
        *(f"| {short[m]} | {ver} |" for m, ver in v.go_modules.items()),
        "",
        "</details>",
        "",
        "## Lines of code",
        "",
        "| Language | Files | Code | Comments | Product | Tests | Tooling |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    total = Row()
    for lang, row in lines.rows.items():
        areas = " | ".join(_n(row.by_area[a]) for a in AREAS)
        out.append(f"| {lang} | {row.files} | {_n(row.code)} | {_n(row.comments)} | {areas} |")
        total.files += row.files
        total.code += row.code
        total.comments += row.comments
        for a in AREAS:
            total.by_area[a] += row.by_area[a]
    areas = " | ".join(f"**{_n(total.by_area[a])}**" for a in AREAS)
    out += [
        f"| **All** | **{total.files}** | **{_n(total.code)}** | **{_n(total.comments)}** "
        f"| {areas} |",
        "",
        "A code line holds something besides comments; a comment line holds only comments, "
        "and in Python docstrings count as comments. Product is what the packages, the "
        "container and the page run; tooling builds, checks and releases them. Left out: "
        "the page's built bundle, the Swagger UI copied in, lockfiles, the word list, test "
        "data and `legacy/`. The docs are another "
        f"{lines.docs_lines:,} lines of Markdown in {lines.docs_files} files.",
    ]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist", help="where the builds are")
    args = parser.parse_args(argv)
    sys.stdout.write(render(versions(args.dist), lines_of_code()))


if __name__ == "__main__":
    main()
