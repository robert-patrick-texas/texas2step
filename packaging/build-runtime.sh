#!/usr/bin/env bash
# Install texas2step's own Python and the application into DEST/python and DEST/venv,
# ready to run from /opt/texas2step. The Debian package (build-deb.sh) and the container
# image (Dockerfile) are both built with this, so they run the same Python and packages.
#
# Python is the version in .python-version: the standalone build uv installs
# (python-build-standalone), which needs only glibc, downloaded and checked by uv. The
# virtualenv on it holds exactly the versions in uv.lock, then the wheel built from this
# source. Needs uv. The web UI is prebuilt under src/texas2step/static, so not Node.
#
#   packaging/build-runtime.sh build/deb/<package>/opt/texas2step    # build-deb.sh
#   packaging/build-runtime.sh /opt/texas2step                       # the Dockerfile
set -euo pipefail

dest=${1:?usage: packaging/build-runtime.sh DEST}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
mkdir -p "$dest"
dest=$(cd "$dest" && pwd)
pydir="$dest/python"
venv="$dest/venv"
if [ -e "$pydir" ] || [ -e "$venv" ]; then
    echo "error: $dest already has a python or venv folder" >&2
    exit 1
fi
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/texas2step/__init__.py)
python_version=$(tr -d '[:space:]' < .python-version)
pyver=${python_version%.*}
wheel=$(mktemp -d)
trap 'rm -rf "$wheel"' EXIT

# 1. Python itself, into build/pythons (kept from one build to the next), then copied.
export UV_PYTHON_INSTALL_DIR=${UV_PYTHON_INSTALL_DIR:-$root/build/pythons}
uv python install --quiet --no-bin "$python_version"
built=$(uv python find --managed-python --no-project "$python_version")
built=$(dirname "$(dirname "$built")")
cp -a "$built" "$pydir"
python="$pydir/bin/python$pyver"

# 2. The application and its locked dependencies in a relocatable virtualenv on it.
uv venv --quiet --relocatable --python "$python" "$venv"
UV_PROJECT_ENVIRONMENT="$venv" uv sync --quiet --locked --no-default-groups \
    --no-install-project --python "$python"
uv build --quiet --wheel --out-dir "$wheel"
# --no-cache: uv would otherwise reuse a wheel it cached for this same version number
# from an earlier build, and ship stale code.
uv pip install --quiet --python "$venv/bin/python" --no-deps --no-index --no-cache \
    --find-links "$wheel" "texas2step==$version"
rm -f "$venv/.lock" "$venv/.gitignore"
# The installed package must be exactly what is in src/ now.
if ! diff -rq --exclude=__pycache__ src/texas2step "$venv/lib/python$pyver/site-packages/texas2step"; then
    echo "error: the installed texas2step differs from src/texas2step" >&2
    exit 1
fi

# 3. Point the virtualenv, and Python's record of where it lives, at /opt/texas2step.
ln -sfn "../../python/bin/python$pyver" "$venv/bin/python"
sed -i "s|^home = .*|home = /opt/texas2step/python/bin|" "$venv/pyvenv.cfg"
sed -i "s|$built|/opt/texas2step/python|g" "$pydir"/lib/python"$pyver"/_sysconfigdata_*.py

# 4. What a server never uses: the tools for building C extensions and embedding Python,
#    Tk, IDLE, pip and the manual. The package's postinst and the Dockerfile byte-compile
#    what is left.
rm -rf "$pydir"/bin/{idle3*,pip*,pydoc3*,python3*-config} "$pydir/include" "$pydir/share/man" \
    "$pydir"/lib/{libpython3*,pkgconfig,itcl*,tcl*,tk*,thread*,libtcl*} \
    "$pydir"/lib/python"$pyver"/{idlelib,tkinter,turtledemo,turtle.py,ensurepip,config-*} \
    "$pydir"/lib/python"$pyver"/lib-dynload/_tkinter.* \
    "$pydir"/lib/python"$pyver"/site-packages/pip*
find "$pydir" -name __pycache__ -type d -prune -exec rm -rf {} +

# Nothing may name where it was built.
for built_in in "$UV_PYTHON_INSTALL_DIR" "$dest"; do
    if [ "$built_in" != /opt/texas2step ] && grep -rqF "$built_in" "$pydir" "$venv"; then
        echo "error: these files still name $built_in:" >&2
        grep -rlF "$built_in" "$pydir" "$venv" >&2
        exit 1
    fi
done
echo "Python $python_version and texas2step $version in $dest"
