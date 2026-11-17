#!/usr/bin/env bash
# Build dist/texas2step-<version>-<revision>-<arch>.deb, and dist/texas2step-setup, the
# operators' setup tool for it. The version comes from src/texas2step/__init__.py, the
# only place it is set.
#
# The package carries its own Python at /opt/texas2step/python, the version in
# .python-version, and a self-contained virtualenv on it at /opt/texas2step/venv with
# compiled wheels (aiortc, cryptography, ...), both made by build-runtime.sh. Build on
# Ubuntu 26.04, on the architecture the package is for. Needs uv, dpkg-deb and objdump
# (binutils); runs lintian too if it is installed.
#
#   packaging/build-deb.sh              # or: make deb
#   REVISION=2 packaging/build-deb.sh   # a packaging-only update of the same version,
#                                       # such as a Python security release
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

revision=${REVISION:-1}
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/texas2step/__init__.py)
python_version=$(tr -d '[:space:]' < .python-version)
pyver=${python_version%.*}
arch=$(dpkg --print-architecture)
name="texas2step-${version}-${revision}-${arch}"
work=build/deb
stage="$work/$name"

echo "building $name.deb (Python $python_version)"
rm -rf "$work"
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" dist

# 1. Python and the application, with its locked dependencies, under /opt/texas2step.
packaging/build-runtime.sh "$stage/opt/texas2step"

# 2. System integration. Files under /etc become conffiles, so local edits survive
#    upgrades.
install -Dm644 deploy/texas2step.service "$stage/usr/lib/systemd/system/texas2step.service"
# Unattended updates: off until the operator enables the timer.
install -Dm644 deploy/texas2step-update.service \
    "$stage/usr/lib/systemd/system/texas2step-update.service"
install -Dm644 deploy/texas2step-update.timer "$stage/usr/lib/systemd/system/texas2step-update.timer"
install -Dm644 deploy/texas2step.default "$stage/etc/default/texas2step"
install -Dm644 src/texas2step/config_template.toml "$stage/etc/texas2step/config.toml"
install -Dm644 deploy/nginx/texas2step-http.conf "$stage/etc/nginx/conf.d/texas2step.conf"
install -Dm644 deploy/nginx/texas2step-log.conf "$stage/etc/nginx/conf.d/texas2step-log.conf"
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
# The newest glibc symbol version any bundled binary needs, Python's own included.
glibc=$(find "$stage/opt" -type f \( -name '*.so*' -o -path "*/python/bin/python$pyver" \) \
    -exec objdump -T {} + 2>/dev/null \
    | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)
sed -e "s/@VERSION@/${version}-${revision}/" -e "s/@ARCH@/$arch/" -e "s/@SIZE@/$size/" \
    -e "s/@GLIBC@/${glibc:-2.34}/" \
    packaging/debian/control > "$stage/DEBIAN/control"
install -m755 packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm "$stage/DEBIAN/"
(cd "$stage" && find etc -type f | sed 's|^|/|' | sort) > "$stage/DEBIAN/conffiles"
(cd "$stage" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum) \
    > "$stage/DEBIAN/md5sums"

dpkg-deb --build --root-owner-group -Zxz "$stage" "dist/$name.deb" >/dev/null
echo "dist/$name.deb ($(du -h "dist/$name.deb" | cut -f1))"

# 5. The operators' setup tool, stamped with this package's file name and checksum so
#    it can tell it was handed the package it was built with. Ship the two together.
sha=$(sha256sum "dist/$name.deb" | cut -d' ' -f1)
sed -e "s/@DEB_NAME@/$name.deb/" -e "s/@DEB_SHA256@/$sha/" packaging/texas2step-setup.py \
    > dist/texas2step-setup
chmod 755 dist/texas2step-setup
echo "dist/texas2step-setup (checks and installs $name.deb)"

if command -v lintian >/dev/null; then
    lintian "dist/$name.deb" || true
fi
