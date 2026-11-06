#!/usr/bin/env bash
set -euo pipefail
STATE="$HOME/.config/sites-dashboard"
PREFIX="${SITES_PREFIX:-$HOME/.local}"
if [ -n "${SITES_BIN:-}" ]; then PREFIX="${SITES_BIN%/bin}"; fi
PYTHON="$PREFIX/share/sites-dashboard/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3
"$PYTHON" - "$STATE" "$@" <<'PY'
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

state = Path(sys.argv[1])
args = sys.argv[2:]
if args not in ([], ['--delete-data']):
    raise SystemExit('Usage: uninstall.sh [--delete-data]')
path = state / 'runtime.json'
if not path.exists():
    print('No recorded source installation. For a Debian package use: sudo apt-get remove sites-dashboard')
    sys.exit(0)
metadata = json.loads(path.read_text())
if metadata.get('uninstalled'):
    print('Already uninstalled; retained user data and runtime metadata.')
    sys.exit(0)
app = Path(metadata['app'])
if app.is_symlink() or not app.is_absolute() or app.name != 'sites-dashboard':
    raise SystemExit('Refusing an unexpected application directory: ' + str(app))
data_targets = []
if args == ['--delete-data']:
    home = Path.home().resolve()
    for key in ('SITES_WEBROOT', 'SITES_BACKUP'):
        target = Path(metadata['environment'][key])
        resolved = target.resolve()
        if (target.is_symlink() or not resolved.is_relative_to(home) or resolved == home
                or app.resolve().is_relative_to(resolved) or state.resolve().is_relative_to(resolved)):
            raise SystemExit('Refusing broad/external data deletion: ' + str(target))
        data_targets.append(target)
sys.path.insert(0, str(app / 'bin'))
from sites_platform import RuntimePaths, ServiceManager, _owned_nginx_plist


def command(args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or 'Command failed: ' + str(args))


try:
    values = metadata['runtime']
    for key in ('brew_prefix', 'nginx_available', 'nginx_enabled', 'php_socket', 'php_binary',
                'brew_binary', 'nginx_launchd_plist'):
        if values.get(key) is not None:
            values[key] = Path(values[key])
    runtime = RuntimePaths(**values)
    manager = ServiceManager(runtime)
    if runtime.platform == 'macos' and metadata.get('nginx_owned'):
        if runtime.nginx_launchd_plist.exists():
            _owned_nginx_plist(runtime)
            command(['sudo', '-v'])
            job = subprocess.run(['launchctl', 'print', f'system/{runtime.nginx_launchd_label}'],
                                 capture_output=True, text=True)
            if not job.returncode:
                result = manager.stop('nginx')
                if result.returncode:
                    raise RuntimeError(result.stderr)
            elif 'Could not find service' not in job.stderr:
                raise RuntimeError(job.stderr)
            command(['sudo', '-n', 'rm', '-f', runtime.nginx_launchd_plist])
    for service in metadata.get('owned_services', []):
        result = manager.stop(service)
        if result.returncode:
            raise RuntimeError(result.stderr)
    retained = []
    for filename, expected in metadata.get('owned_files', {}).items():
        target = Path(filename)
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            retained.append(str(target))
            continue
        target.unlink()
    if metadata.get('mailpit_unit'):
        unit = Path('/etc/systemd/system/mailpit.service')
        if unit.exists() and hashlib.sha256(unit.read_bytes()).hexdigest() == metadata['mailpit_unit']:
            command(['sudo', '-n', 'systemctl', 'disable', '--now', 'mailpit'])
            command(['sudo', '-n', 'rm', '-f', unit])
            command(['sudo', '-n', 'systemctl', 'daemon-reload'])
    if runtime.platform == 'macos' and metadata.get('php_pool') and runtime.php_service not in metadata.get('owned_services', []):
        result = manager.restart(runtime.php_service)
        if result.returncode:
            raise RuntimeError(result.stderr)
    begin, end = '# >>> sites-dashboard >>>', '# <<< sites-dashboard <<<'
    for filename in metadata.get('profiles', []):
        profile = Path(filename)
        if profile.is_symlink() or not profile.exists():
            continue
        content = profile.read_text()
        if content.count(begin) == 1 and content.count(end) == 1 and content.index(begin) < content.index(end):
            previous = metadata.get('profile_blocks', {}).get(str(profile), '')
            owned_lines = set(previous.splitlines(keepends=True))
            def remove_owned(match):
                return ''.join(line for line in match.group().splitlines(keepends=True)
                               if line not in owned_lines and line.strip() not in (begin, end))
            profile.write_text(re.sub(re.escape(begin) + r'.*?' + re.escape(end) + r'\n?',
                                     remove_owned, content, flags=re.S))
    venv = app / 'venv'
    if venv.is_symlink() or app.is_symlink() or app.name != 'sites-dashboard':
        raise RuntimeError('Refusing an unexpected application directory: ' + str(app))
    if venv.exists():
        shutil.rmtree(venv)
    for target in data_targets:
        if target.exists():
            shutil.rmtree(target)
    metadata['uninstalled'] = True
    path.write_text(json.dumps(metadata, indent=2, default=str))
    print('Uninstalled. Sites, backups, certificates and databases preserved.' if not args else
          'Uninstalled. Configured sites/backups deleted; certificates/databases require manual removal.')
    if retained:
        print('Preserved edited files: ' + ', '.join(retained))
    # Retain metadata as a recovery/audit record; never purge user configuration.
except (OSError, ValueError, KeyError, RuntimeError) as error:
    print('Uninstall failed: ' + str(error), file=sys.stderr)
    sys.exit(1)
PY
