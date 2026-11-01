#!/usr/bin/env bash
# Build the t2s package for each Linux architecture t2s is built for,
# DIST/t2s_<version>-<revision>_amd64.deb and _arm64.deb, from DIST/t2s-linux-amd64 and
# DIST/t2s-linux-arm64, the files make t2s builds. The version comes from
# src/texas2step/__init__.py, the only place it is set. The names are Debian's, with
# underscores, so they never match texas2step-*-amd64.deb, the server's package, which
# docs/install.md finds in a release by that pattern.
#
# Each package holds t2s at /usr/bin/t2s, its bash completion and its man page, which t2s
# writes itself (t2s __share), its copyright, and modules.txt: the Go modules built into
# t2s, each with its license's text. Needs Go (nfpm runs through go run, at the version
# pinned below) and gzip; runs lintian too if it is installed.
#
#   packaging/build-t2s-deb.sh              # or: make t2s-deb, after make t2s
#   DIST=/some/dir packaging/build-t2s-deb.sh
#   REVISION=2 packaging/build-t2s-deb.sh   # a packaging-only update of the same version
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

nfpm=(go run github.com/goreleaser/nfpm/v2/cmd/nfpm@v2.48.0)
arches=(amd64 arm64)
revision=${REVISION:-1}
dist=${DIST:-dist}
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/texas2step/__init__.py)
host=$(dpkg --print-architecture)
work=build/t2s-deb
share="$work/share"

# 1. The files must be this version's, from one make t2s: the one this computer runs must
#    say it is t2s <version> (Go keeps -ldflags out of the build information with
#    -trimpath), and each was built from the same commit, changed or not, as that one.
source_of() { go version -m "$1" | sed -n 's/^\tbuild\tvcs\.\(revision\|modified\)=//p' | paste -sd ' '; }
if [ ! -x "$dist/t2s-linux-$host" ]; then
    echo "error: no $dist/t2s-linux-$host, which this computer ($host) runs to write the man page: run make t2s" >&2
    exit 1
fi
if [ "$("$dist/t2s-linux-$host" version)" != "t2s $version" ]; then
    echo "error: $dist/t2s-linux-$host isn't t2s $version: run make t2s" >&2
    exit 1
fi
built=$(source_of "$dist/t2s-linux-$host")
for arch in "${arches[@]}"; do
    bin="$dist/t2s-linux-$arch"
    if [ ! -f "$bin" ]; then
        echo "error: no $bin: run make t2s first" >&2
        exit 1
    fi
    if [ "$(source_of "$bin")" != "$built" ]; then
        echo "error: $bin was built from another source than $dist/t2s-linux-$host: run make t2s" >&2
        exit 1
    fi
done

# 2. File times and the changelog's date from the last commit, so a rebuild is the same.
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}

# 3. The bash completion and the man page, from t2s itself.
rm -rf "$work"
mkdir -p "$share/doc"
"$dist/t2s-linux-$host" __share "$share"
gzip -9n "$share/man/man1/t2s.1"
cat > "$work/changelog" <<EOF
t2s (${version}-${revision}) unstable; urgency=medium

  * Release ${version}. See https://github.com/robert-patrick-texas/texas2step

 -- Robert Patrick <193846785+robert-patrick-texas@users.noreply.github.com>  $(date -R -u -d "@$SOURCE_DATE_EPOCH")
EOF
gzip -9n < "$work/changelog" > "$share/doc/changelog.Debian.gz"

# 4. modules.txt: the Go standard library and each module linked into t2s on Linux, with
#    the license file each one carries.
goversion=$(go version -m "$dist/t2s-linux-$host" | sed -n '1s/.*: //p')
{
    echo "The Go standard library and the Go modules built into t2s $version, each with"
    echo "its license. t2s's own license is in /usr/share/doc/t2s/copyright."
    printf '\n== The Go standard library, %s\n\n' "$goversion"
    cat "$(go env GOROOT)/LICENSE"
    for arch in "${arches[@]}"; do
        (cd t2s && GOOS=linux GOARCH=$arch go list -deps \
            -f '{{with .Module}}{{if not .Main}}{{.Path}} {{.Version}} {{.Dir}}{{end}}{{end}}' .)
    done | sort -u | while read -r path modversion dir; do
        license=
        for name in LICENSE LICENSE.md LICENSE.txt COPYING; do
            if [ -f "$dir/$name" ]; then
                license="$dir/$name"
                break
            fi
        done
        if [ -z "$license" ]; then
            echo "error: $path $modversion has no license file in $dir" >&2
            exit 1
        fi
        printf '\n== %s %s\n\n' "$path" "$modversion"
        cat "$license"
    done
} > "$share/doc/modules.txt"

# 5. A package for each architecture.
for arch in "${arches[@]}"; do
    out="$dist/t2s_${version}-${revision}_${arch}.deb"
    T2S_ARCH=$arch T2S_VERSION=$version T2S_REVISION=$revision T2S_BIN="$dist/t2s-linux-$arch" \
        T2S_SHARE="$share" "${nfpm[@]}" package --config packaging/t2s/nfpm.yaml --packager deb \
        --target "$out" >/dev/null
    echo "$out ($(du -h "$out" | cut -f1))"
done

if command -v lintian >/dev/null; then
    for arch in "${arches[@]}"; do
        lintian "$dist/t2s_${version}-${revision}_${arch}.deb" || true
    done
fi
