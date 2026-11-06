#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-1.2.0}"
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.+:~_-]*$ ]] || { echo 'Invalid Debian version.' >&2; exit 1; }
PKGNAME="sites-dashboard_${VERSION}_all.deb"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/sites-dashboard-deb.XXXXXXXX")
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/local/bin"
chmod 755 "$STAGE" "$STAGE/DEBIAN" "$STAGE/usr" "$STAGE/usr/local" "$STAGE/usr/local/bin"
install -m 755 bin/sites bin/newsite "$STAGE/usr/local/bin/"
install -m 644 bin/sites_platform.py bin/sites_config.py "$STAGE/usr/local/bin/"
sed -e "s/^Version: .*/Version: ${VERSION}/" \
    -e '/^Depends:/ s/$/, python3-typer, python3-rich, python3-textual/' \
    DEBIAN/control > "$STAGE/DEBIAN/control"
install -m 755 DEBIAN/postinst DEBIAN/prerm "$STAGE/DEBIAN/"
dpkg-deb --build --root-owner-group "$STAGE" "$PKGNAME"
echo "Done: $PKGNAME"
