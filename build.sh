#!/usr/bin/env bash
# build.sh - build sites-dashboard .deb package
# Usage: ./build.sh [version]
set -e
cd "$(dirname "$0")"

VERSION="${1:-1.2.0}"
PKGNAME="sites-dashboard_${VERSION}_all.deb"

echo "==> Staging"
rm -rf .debstage
mkdir -p .debstage/DEBIAN .debstage/usr/local/bin
cp bin/sites bin/newsite .debstage/usr/local/bin/
chmod 755 .debstage/usr/local/bin/sites .debstage/usr/local/bin/newsite
sed "s/^Version: .*/Version: ${VERSION}/" DEBIAN/control > .debstage/DEBIAN/control
cp DEBIAN/postinst DEBIAN/prerm .debstage/DEBIAN/
chmod 755 .debstage/DEBIAN/postinst .debstage/DEBIAN/prerm

echo "==> Building ${PKGNAME}"
dpkg-deb --build --root-owner-group .debstage "${PKGNAME}"
rm -rf .debstage
echo "==> Done: ${PKGNAME}"
ls -lh "${PKGNAME}"