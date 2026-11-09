"""scripts/release_facts.py: the versions and lines of code in a release's notes."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_facts.py"


def _script():
    spec = importlib.util.spec_from_file_location("release_facts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # for its dataclasses
    spec.loader.exec_module(module)
    return module


facts = _script()


def test_python_docstrings_and_comments_are_comments():
    text = '''"""A module's docstring,
on two lines."""

import os  # a comment after code is code

# a comment of its own


def f():
    """And a function's."""
    words = """a string that is
    assigned is code"""
    return os.sep + words
'''
    # Code: import, def, the assignment's two lines, return. Comments: the docstrings'
    # three lines and the comment of its own.
    assert facts.python_lines(text) == (5, 4)


def test_slashes_in_strings_are_not_comments():
    go = """package main

/* a block
   comment */ var a = 1
// a line comment
var b = "// not a comment" + `raw \\` // this one is
// a comment, as the raw string ended at its backtick
var c = '/' /* and this */
"""
    assert facts.c_lines(go, raw_backticks=True) == (4, 3)
    # Read as escaped, the backtick would go on, to the end: no comment on line 7.
    assert facts.c_lines(go) == (5, 2)
    ts = "const u = `http://${host}/x`; // a comment\n/** doc */\nlet q = 'it\\'s // fine';\n"
    assert facts.c_lines(ts) == (2, 1)
    css = ".a { background: url(http://x/y.png); } /* a comment */\n/*\n * more\n */\n"
    assert facts.c_lines(css, slashes=False) == (1, 3)


def test_hash_and_html_comments():
    assert facts.hash_lines("#!/bin/sh\n# says hello\n\necho hi  # and more\n") == (1, 2)
    html = "<!doctype html>\n<!-- one -->\n<!-- two\n three -->\n<p>a</p> <!-- x --> <p>b</p>\n"
    assert facts.html_lines(html) == (2, 3)


def test_each_file_has_its_language_and_area():
    cases = {
        "src/texas2step/app.py": ("Python", "product"),
        "tests/test_api.py": ("Python", "tests"),
        "scripts/build_docs.py": ("Python", "tooling"),
        "web/src/ui/app.ts": ("TypeScript", "product"),
        "web/build.mjs": ("JavaScript", "tooling"),
        "t2s/main.go": ("Go", "product"),
        "t2s/main_test.go": ("Go", "tests"),
        "packaging/build-deb.sh": ("Shell", "tooling"),
        "deploy/container/entrypoint.sh": ("Shell", "product"),
        "Makefile": ("Make", "tooling"),
        "Dockerfile": ("Dockerfile", "tooling"),
        ".github/workflows/pypi.yml": ("YAML", "tooling"),
        "deploy/nginx/texas2step.conf": ("nginx", "product"),
        "deploy/texas2step.service": ("systemd", "product"),
    }
    for path, (lang, where) in cases.items():
        assert (facts.language(path), facts.area(path)) == (lang, where), path
    # A maintainer script, by its first line: it runs where the package is installed.
    assert facts.language("packaging/debian/postinst", "#!/bin/sh") == "Shell"
    assert facts.area("packaging/debian/postinst") == "product"
    assert facts.language("packaging/debian/control", "Source: texas2step") is None
    # Built, copied in, locked, data or the old version: not counted.
    for path in (
        "src/texas2step/static/assets/app.js",
        "src/texas2step/static/assets/swagger/swagger-ui-bundle.js",
        "uv.lock",
        "t2s/go.sum",
        "web/package-lock.json",
        "src/texas2step/words.txt",
        "legacy/app.py",
    ):
        assert facts.language(path) is None, path
    assert facts.is_docs("docs/access.md") and facts.is_docs("README.md")
    assert not facts.is_docs("LICENSE.md") and not facts.is_docs("legacy/README.md")


def test_versions_are_read_from_what_was_built():
    listing = (
        "drwxr-xr-x root/root 0 2026-11-09 ./usr/lib/texas2step/lib/python3.14/site-packages/"
        "aiortc-1.16.0.dist-info/\n"
        "drwxr-xr-x root/root 0 2026-11-09 ./usr/lib/texas2step/lib/python3.14/site-packages/"
        "typing_extensions-4.16.0.dist-info/\n"
        "-rw-r--r-- root/root 9 2026-11-09 ./usr/lib/texas2step/lib/python3.14/site-packages/"
        "aiortc-1.16.0.dist-info/METADATA\n"
    )
    assert facts.deb_packages(listing) == {"aiortc": "1.16.0", "typing-extensions": "4.16.0"}
    depends = "python3 (>= 3.14), python3 (<< 3.15), libc6 (>= 2.42), adduser"
    assert facts.deb_python(depends) == "3.14"
    info = (
        "dist/t2s-linux-amd64: go1.27.3\n"
        "\tpath\tgithub.com/robert-patrick-texas/texas2step/t2s\n"
        "\tdep\tgithub.com/pion/webrtc/v4\tv4.2.26\th1:x=\n"
        "\tdep\tgolang.org/x/sys\tv0.50.0\th1:y=\n"
        "\tbuild\tGOOS=linux\n"
    )
    assert facts.go_build(info) == (
        "1.27.3",
        {"github.com/pion/webrtc/v4": "4.2.26", "golang.org/x/sys": "0.50.0"},
    )
    lock = {"packages": {"node_modules/typescript": {"version": "7.0.2"}}}
    assert facts.lock_versions(lock, ["typescript", "esbuild"]) == {
        "typescript": "7.0.2",
        "esbuild": "?",
    }


def test_the_projects_own_files_name_their_versions():
    assert facts.version() == facts.version(ROOT) and facts.version().count(".") == 2
    assert "aiortc" in facts.direct_python() and "fastapi" in facts.direct_python()
    direct = facts.direct_go()
    assert "github.com/pion/webrtc/v4" in direct
    assert not any("pion/dtls" in m for m in direct)  # indirect
    pinned = facts.pinned()
    assert pinned["ubuntu"].startswith("ubuntu:") and "?" not in pinned.values(), pinned


def test_the_repo_counts_and_the_notes_read_well():
    lines = facts.lines_of_code()
    assert list(lines.rows)[:5] == ["Python", "TypeScript", "Go", "Shell", "Make"]
    assert all(row.code > 0 and row.files > 0 for row in lines.rows.values())
    python = lines.rows["Python"]
    assert python.by_area["product"] and python.by_area["tests"] and python.by_area["tooling"]
    assert sum(python.by_area.values()) == python.code
    assert lines.docs_files > 10 and lines.docs_lines > 1000
    versions = facts.Versions(
        python="3.14",
        python_packages={"aiortc": "1.16.0", "fastapi": "0.162.2", "pyee": "16.0.1"},
        python_direct=["aiortc", "fastapi"],
        go="1.27.3",
        go_modules={"github.com/pion/webrtc/v4": "4.2.26", "golang.org/x/text": "0.44.0"},
        go_direct=["github.com/pion/webrtc/v4"],
        pinned={
            "ubuntu": "ubuntu:26.04",
            "uv": "0.13.6",
            "nfpm": "2.48.0",
            "typescript": "7.0.2",
            "esbuild": "0.28.2",
            "swagger-ui-dist": "5.34.1",
        },
        node="24.22.1",
    )
    notes = facts.render(versions, lines)
    assert notes.startswith("## Versions\n")
    assert "| Its Python packages | aiortc 1.16.0, fastapi 0.162.2, and the 1 they need |" in notes
    assert "| Its Go modules | pion/webrtc/v4 4.2.26, and the 1 they need |" in notes
    assert "<details><summary>Every package: 3 Python, 2 Go</summary>" in notes
    assert "| golang.org/x/text | 0.44.0 |" in notes
    assert "\n## Lines of code\n" in notes and "| **All** |" in notes
    assert "?" not in notes
