#!/usr/bin/env bash
# Build dist/texas2step_<version>-<revision>_<arch>.deb.
#
# The package carries a self-contained virtualenv at /opt/texas2step/venv that runs on
# the system Python and holds compiled wheels (aiortc, cryptography, ...). So build on
# the release the package is for: Ubuntu 26.04 (Python 3.14) on the same architecture.
# Needs uv, dpkg-deb and objdump (binutils); runs lintian too if it is installed.
#
#   packaging/build-deb.sh              # or: make deb
#   REVISION=2 packaging/build-deb.sh   # a packaging-only update of the same version
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

python=${PYTHON:-/usr/bin/python3}
revision=${REVISION:-1}
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/texas2step/__init__.py)
arch=$(dpkg --print-architecture)
read -r pyver pynext < <("$python" -c 'import sys; v = sys.version_info; print(f"{v[0]}.{v[1]} {v[0]}.{v[1] + 1}")')
name="texas2step_${version}-${revision}_${arch}"
work=build/deb
stage="$work/$name"
venv="$stage/opt/texas2step/venv"

echo "building $name.deb (Python $pyver from $python)"
rm -rf "$work"
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" dist

# 1. The application and its locked dependencies in a relocatable virtualenv. The
#    web UI is prebuilt under src/texas2step/static, so Node is not needed here.
uv build --quiet --wheel --out-dir "$work/wheel"
uv export --quiet --frozen --no-default-groups --no-emit-project --format requirements-txt \
    --output-file "$work/requirements.txt"
uv venv --quiet --relocatable --python "$python" "$venv"
uv pip install --quiet --python "$venv/bin/python" -r "$work/requirements.txt"
uv pip install --quiet --python "$venv/bin/python" --no-deps --no-index \
    --find-links "$work/wheel" "texas2step==$version"
rm -f "$venv/.lock" "$venv/.gitignore"

# 2. System integration. Files under /etc become conffiles, so local edits survive
#    upgrades.
install -Dm644 deploy/texas2step.service "$stage/usr/lib/systemd/system/texas2step.service"
install -Dm644 deploy/texas2step.default "$stage/etc/default/texas2step"
install -Dm644 deploy/config.toml "$stage/etc/texas2step/config.toml"
install -Dm644 deploy/nginx/texas2step-http.conf "$stage/etc/nginx/conf.d/texas2step.conf"
install -Dm644 deploy/nginx/texas2step.conf "$stage/etc/nginx/snippets/texas2step.conf"
install -Dm644 deploy/nginx/texas2step-speedtest.conf \
    "$stage/etc/nginx/snippets/texas2step-speedtest.conf"
ln -s /opt/texas2step/venv/bin/texas2step "$stage/usr/bin/texas2step"

doc="$stage/usr/share/doc/texas2step"
install -Dm644 packaging/debian/copyright "$doc/copyright"
install -Dm644 packaging/debian/lintian-overrides "$stage/usr/share/lintian/overrides/texas2step"
install -Dm644 README.md "$doc/README.md"
cp -r docs "$doc/docs"
cat > "$work/changelog" <<EOF
texas2step (${version}-${revision}) unstable; urgency=medium

  * Release ${version}. See https://github.com/robert-patrick-texas/texas2step

 -- Robert Patrick <193846785+robert-patrick-texas@users.noreply.github.com>  $(date -R)
EOF
gzip -9n < "$work/changelog" > "$doc/changelog.Debian.gz"

# 3. Normal permissions, whatever the build host's umask.
find "$stage" -type d -exec chmod 755 {} +
find "$stage" -type f -perm /111 -exec chmod 755 {} +
find "$stage" -type f ! -perm /111 -exec chmod 644 {} +
find "$stage/opt" -type f \( -name '*.so' -o -name '*.so.*' -o -name '*.py' \) -exec chmod 644 {} +

# 4. Package metadata.
size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
# The newest glibc symbol version any bundled binary needs.
glibc=$(find "$stage/opt" -type f -name '*.so*' -exec objdump -T {} + 2>/dev/null \
    | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)
sed -e "s/@VERSION@/${version}-${revision}/" -e "s/@ARCH@/$arch/" -e "s/@SIZE@/$size/" \
    -e "s/@PYVER@/$pyver/" -e "s/@PYNEXT@/$pynext/" -e "s/@GLIBC@/${glibc:-2.34}/" \
    packaging/debian/control > "$stage/DEBIAN/control"
install -m755 packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm "$stage/DEBIAN/"
(cd "$stage" && find etc -type f | sed 's|^|/|' | sort) > "$stage/DEBIAN/conffiles"
(cd "$stage" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum) \
    > "$stage/DEBIAN/md5sums"

dpkg-deb --build --root-owner-group -Zxz "$stage" "dist/$name.deb" >/dev/null
echo "dist/$name.deb ($(du -h "dist/$name.deb" | cut -f1))"

if command -v lintian >/dev/null; then
    lintian "dist/$name.deb" || true
fi
