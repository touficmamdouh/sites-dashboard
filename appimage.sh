#!/usr/bin/env bash
# appimage.sh - build Sites Dashboard AppImage from the repo
# Usage: ./appimage.sh [version] [repo-dir]   (defaults: 1.2.0, git clone of v<version>)
#
# Downloads (once, cached in .appimage-cache/ next to this script):
#   - appimagetool (continuous)
#   - CPython 3.12 standalone (astral-sh/python-build-standalone)
# Needs: curl, tar. Output: Sites-Dashboard-<ver>-x86_64.AppImage (next to this script)
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

VERSION="${1:-1.2.0}"
if [ -n "$2" ]; then
  REPO="$2"
else
  REPO="$SCRIPT_DIR/repo-src"
  [ -d "$REPO/.git" ] || git clone --branch "v$VERSION" https://github.com/touficmamdouh/sites-dashboard.git "$REPO"
fi

ARCH="x86_64"
APPID="Sites-Dashboard"
CACHE="$SCRIPT_DIR/.appimage-cache"
WORK="$CACHE/work"
APPDIR="$WORK/$APPID.AppDir"
OUT="$SCRIPT_DIR/$APPID-${VERSION}-${ARCH}.AppImage"

APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
CPYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.12.15%2B20261013-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"

mkdir -p "$CACHE"
[ -f "$CACHE/appimagetool.AppImage" ] || { echo "==> downloading appimagetool"; curl -sSL -C - --retry 3 -o "$CACHE/appimagetool.AppImage" "$APPIMAGETOOL_URL"; chmod +x "$CACHE/appimagetool.AppImage"; }
[ -f "$CACHE/cpython.tar.gz" ] || { echo "==> downloading cpython"; curl -sSL -C - --retry 3 -o "$CACHE/cpython.tar.gz" "$CPYTHON_URL"; }

echo "==> staging AppDir"
rm -rf "$WORK"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
tar xzf "$CACHE/cpython.tar.gz" -C "$APPDIR/usr"   # yields usr/python/
if [ ! -x "$APPDIR/usr/python/bin/python3" ]; then
  # normalize unexpected top-level layout
  sub="$(find "$APPDIR/usr" -maxdepth 3 -path '*/bin/python3' -type f | head -1)"
  [ -n "$sub" ] && { pydir="$(dirname "$(dirname "$sub")")"; mv "$pydir"/* "$APPDIR/usr/python"/ 2>/dev/null || true; }
fi
"$APPDIR/usr/python/bin/python3" --version

echo "==> pip install typer rich textual"
"$APPDIR/usr/python/bin/python3" -m pip install --quiet --upgrade pip
"$APPDIR/usr/python/bin/python3" -m pip install --quiet typer rich textual

echo "==> copy app scripts from $REPO"
cp "$REPO/bin/sites" "$REPO/bin/newsite" "$APPDIR/usr/bin/"
chmod 755 "$APPDIR/usr/bin/sites" "$APPDIR/usr/bin/newsite"
"$APPDIR/usr/python/bin/python3" -m py_compile "$APPDIR/usr/bin/sites"

echo "==> icon"
"$APPDIR/usr/python/bin/python3" - "$APPDIR/usr/share/icons/hicolor/256x256/apps/sites-dashboard.png" <<'PY'
import struct, zlib, sys
out = sys.argv[1]
W = H = 256
def chunk(t, d):
    c = t + d
    return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
def px(x, y):
    # dark slate bg, green panel with dark screen, amber status dot
    bg = (30, 41, 59)
    if 40 < x < 216 and 60 < y < 196:
        bg = (34, 197, 94) if not (80 < x < 176 and 92 < y < 164) else (15, 23, 42)
    ex, ey = x - 176, y - 80
    if ex*ex + ey*ey < 26*26:
        bg = (251, 191, 36)
    return bytes((*bg, 255))
raw = b''.join(b'\x00' + b''.join(px(x, y) for x in range(W)) for y in range(H))
png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 6, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
open(out, 'wb').write(png)
print('icon written')
PY
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/sites-dashboard.png" "$APPDIR/.DirIcon"
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/sites-dashboard.png" "$APPDIR/sites-dashboard.png"

cat > "$APPDIR/sites-dashboard.desktop" <<'DESK'
[Desktop Entry]
Name=Sites Dashboard
Comment=Local web server manager (Nginx + PHP-FPM + MariaDB)
Exec=sites
Icon=sites-dashboard
Terminal=true
Type=Application
Categories=System;Monitor;
StartupWMClass=sites-dashboard
DESK
cp "$APPDIR/sites-dashboard.desktop" "$APPDIR/usr/share/applications/"

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/bash
# AppRun - launch bundled sites tool (defaults to TUI dashboard)
HERE="$(dirname "$(readlink -f "$0")")"
unset PYTHONHOME PYTHONPATH
if [ $# -eq 0 ]; then
  set -- dashboard
fi
exec "$HERE/usr/python/bin/python3" "$HERE/usr/bin/sites" "$@"
APPRUN
chmod 755 "$APPDIR/AppRun"

echo "==> build AppImage"
ARCH="$ARCH" "$CACHE/appimagetool.AppImage" "$APPDIR" "$OUT"
ls -lh "$OUT"
