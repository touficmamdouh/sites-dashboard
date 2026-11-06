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
mkdir -p "$STAGE/usr/lib/sites-dashboard" "$STAGE/usr/lib/systemd/system" "$STAGE/usr/share/sites-dashboard"
chmod 755 "$STAGE/usr/lib" "$STAGE/usr/lib/sites-dashboard" "$STAGE/usr/lib/systemd" \
    "$STAGE/usr/lib/systemd/system" "$STAGE/usr/share" "$STAGE/usr/share/sites-dashboard"
install -m 755 DEBIAN/postinst "$STAGE/usr/lib/sites-dashboard/package-lifecycle"
python3 - "$STAGE" <<'PY'
import sys
from pathlib import Path
stage = Path(sys.argv[1])
(stage / 'usr/lib/systemd/system/sites-dashboard-mailpit.service').write_text('''[Unit]
Description=Sites Dashboard local mail capture
After=network.target
[Service]
DynamicUser=yes
StateDirectory=sites-dashboard-mailpit
WorkingDirectory=/var/lib/sites-dashboard-mailpit
ExecStart=/var/lib/sites-dashboard/bin/mailpit --smtp 127.0.0.1:1025 --listen 127.0.0.1:8025 --database /var/lib/sites-dashboard-mailpit/mailpit.db
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
[Install]
WantedBy=multi-user.target
Alias=mailpit.service
''')
(stage / 'usr/share/sites-dashboard/msmtprc').write_text('''# sites-dashboard local relay
defaults
auth off
tls off
account local
host 127.0.0.1
port 1025
from noreply@localhost
account default : local
''')
PY
chmod 644 "$STAGE/usr/lib/systemd/system/sites-dashboard-mailpit.service" "$STAGE/usr/share/sites-dashboard/msmtprc"
dpkg-deb --build --root-owner-group "$STAGE" "$PKGNAME"
echo "Done: $PKGNAME"
