#!/usr/bin/env bash
# Install as the login user; sudo is limited to system setup.
set -euo pipefail
SOURCE=$(cd "$(dirname "$0")" && pwd)
STATE="$HOME/.config/sites-dashboard"
PREFIX="${SITES_PREFIX:-$HOME/.local}"
if [ -n "${SITES_BIN:-}" ]; then PREFIX="${SITES_BIN%/bin}"; fi
APP="$PREFIX/share/sites-dashboard"

manual_brew() {
    echo 'Homebrew is required. Run these commands, then rerun ./install.sh:' >&2
    echo '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
    printf 'eval "$(%s/bin/brew shellenv)"\n' "${SITES_BREW_PREFIX:-$DEFAULT_BREW}" >&2
}

case "$(uname -s)" in
Darwin)
    VERSION=$(sw_vers -productVersion)
    [ "${VERSION%%.*}" -ge 13 ] || { echo 'macOS 13 or newer is required.' >&2; exit 1; }
    case "$(uname -m)" in
        arm64) DEFAULT_BREW=/opt/homebrew ;;
        x86_64) DEFAULT_BREW=/usr/local ;;
        *) echo 'Unsupported macOS architecture; use Apple Silicon or Intel.' >&2; exit 1 ;;
    esac
    [ "$(id -u)" != 0 ] || { echo 'Run as the login user, without sudo.' >&2; exit 1; }
    BREW=''
    if [ -n "${SITES_BREW_PREFIX:-}" ]; then
        [ ! -x "$SITES_BREW_PREFIX/bin/brew" ] || BREW="$SITES_BREW_PREFIX/bin/brew"
    else
        BREW=$(command -v brew || true)
        [ -n "$BREW" ] || { [ ! -x "$DEFAULT_BREW/bin/brew" ] || BREW="$DEFAULT_BREW/bin/brew"; }
    fi
    if [ -z "$BREW" ]; then
        if [ ! -t 0 ]; then manual_brew; exit 1; fi
        printf 'Install Homebrew using its official installer? [y/N] '
        read -r CONSENT
        case "$CONSENT" in
            y|Y|yes|YES)
                BREW_INSTALLER=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)
                /bin/bash -c "$BREW_INSTALLER"
                BREW="${SITES_BREW_PREFIX:-$DEFAULT_BREW}/bin/brew"
                [ -x "$BREW" ] || { manual_brew; exit 1; }
                ;;
            *) manual_brew; exit 1 ;;
        esac
    fi
    SITES_BREW_PREFIX=$("$BREW" --prefix)
    export SITES_BREW_PREFIX
    export PATH="$SITES_BREW_PREFIX/bin:$SITES_BREW_PREFIX/sbin:$PATH"
    for FORMULA in python nginx "${SITES_PHP_SERVICE:-${SITES_PHP_SVC:-php}}" mariadb mailpit mkcert curl wget; do
        "$BREW" list --versions "$FORMULA" >/dev/null 2>&1 || "$BREW" install "$FORMULA"
    done
    PYTHON="$SITES_BREW_PREFIX/bin/python3"
    PLATFORM=macos
    ;;
Linux)
    command -v apt-get >/dev/null || { echo 'Linux installation requires Debian/Ubuntu (apt-get).' >&2; exit 1; }
    sudo -v
    sudo -n apt-get update -qq
    sudo -n apt-get install -y python3 python3-venv python3-pip nginx mariadb-server php-fpm \
        curl tar wget msmtp-mta ca-certificates certbot python3-certbot-nginx
    PYTHON=python3
    PLATFORM=linux
    ;;
*) echo 'Only Debian/Ubuntu Linux and macOS are supported.' >&2; exit 1 ;;
esac

mkdir -p "$APP" "$STATE"
[ -x "$APP/venv/bin/python" ] || "$PYTHON" -m venv "$APP/venv"
"$APP/venv/bin/python" -m pip install --upgrade typer rich textual
"$APP/venv/bin/python" - "$SOURCE" "$APP" "$STATE" "$PLATFORM" <<'PY'
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

source, app, state = map(Path, sys.argv[1:4])
platform_name = sys.argv[4]
sys.path.insert(0, str(source / 'bin'))
from sites_config import load_config
from sites_platform import ServiceManager, install_nginx_service


def command(args):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stderr or result.stdout}")
    return result.stdout.strip()


def digest(content):
    return hashlib.sha256(content).hexdigest()


metadata_path = state / 'runtime.json'
metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
if not isinstance(metadata, dict):
    raise SystemExit('runtime.json must contain an object; inspect it before retrying.')
if metadata.get('app') and metadata['app'] != str(app):
    raise SystemExit('An installation already exists at ' + metadata['app'] + '; reuse its SITES_PREFIX.')
files = metadata.setdefault('owned_files', {})
services = metadata.setdefault('owned_services', [])
pending = metadata.setdefault('pending_files', {})
metadata.update(app=str(app), platform=platform_name, ready=False, uninstalled=False)


def save():
    with tempfile.NamedTemporaryFile(mode='w', dir=state, delete=False) as f:
        json.dump(metadata, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
        temporary = Path(f.name)
    temporary.chmod(0o600)
    temporary.replace(metadata_path)


def recover_replacement(path):
    record = pending.get(str(path))
    if record is None:
        return
    if path.is_symlink():
        raise RuntimeError(f'Refusing interrupted replacement symlink: {path}')
    current = digest(path.read_bytes()) if path.exists() else None
    if current not in (record['old'], record['new']):
        raise RuntimeError(f'Interrupted replacement was edited: {path}')
    if current == record['new']:
        files[str(path)] = record['new']
    staged = Path(record['temporary'])
    if staged.parent != path.parent or not staged.name.startswith('.sites-dashboard-') or staged.is_symlink():
        raise RuntimeError(f'Invalid replacement staging path: {staged}')
    staged.unlink(missing_ok=True)
    del pending[str(path)]
    save()


def write_owned(path, content, mode=0o644, preserve_edits=False):
    path = Path(path)
    content = content.encode() if isinstance(content, str) else content
    recover_replacement(path)
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlink: {path}')
    if path.exists():
        current = path.read_bytes()
        if files.get(str(path)) != digest(current):
            if preserve_edits and str(path) in files:
                print(f'Preserving edited configuration: {path}')
                return False
            raise RuntimeError(f'Refusing to overwrite unowned or edited file: {path}')
        if current == content:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.sites-dashboard-', delete=False) as f:
        temporary = Path(f.name)
        pending[str(path)] = {'old': files.get(str(path)), 'new': digest(content), 'temporary': str(temporary)}
        save()
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    temporary.chmod(mode)
    temporary.replace(path)
    files[str(path)] = digest(content)
    del pending[str(path)]
    save()
    return True


def check(result):
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)


def wait_ready(name, probe):
    error = ''
    for _ in range(20):
        try:
            probe()
            return
        except (OSError, RuntimeError) as exc:
            error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f'{name} is not ready: {error}. Check its service logs and rerun ./install.sh.')


def tcp(port):
    with socket.create_connection(('127.0.0.1', port), timeout=1):
        pass


def php_ready():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(str(config.runtime.php_socket))


def quote_nginx(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise RuntimeError('Configuration paths must not contain control characters.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


try:
    for filename in list(pending):
        recover_replacement(Path(filename))
    metadata.pop('cleanup_runtime_done', None)
    for key, value in metadata.get('environment', {}).items():
        os.environ.setdefault(key, value)
    if platform_name == 'macos':
        if not os.environ.get('SITES_PHP_SERVICE') and not os.environ.get('SITES_PHP_SVC'):
            candidates = json.loads(command([Path(os.environ['SITES_BREW_PREFIX']) / 'bin/brew',
                                             'services', 'list', '--json']))
            candidates = [item for item in candidates if re.fullmatch(r'php(?:@[0-9.]+)?', item.get('name', ''))]
            candidates.sort(key=lambda item: item.get('status') != 'started')
            os.environ['SITES_PHP_SERVICE'] = candidates[0]['name'] if candidates else 'php'
        own_pool = not os.environ.get('SITES_PHP_SOCK') or metadata.get('php_pool') is not None
        os.environ.setdefault('SITES_PHP_SOCK', str(app / 'run/php-fpm.sock'))
    config = load_config()
    runtime = config.runtime
    manager = ServiceManager(runtime)
    metadata['runtime'] = asdict(runtime)
    metadata['environment'] = {key: str(value) for key, value in {
        'SITES_PHP_SERVICE': runtime.php_service, 'SITES_PHP_SOCK': runtime.php_socket,
        'SITES_NGINX_PREFIX': runtime.nginx_available.parent,
        'SITES_WEBROOT': config.webroot, 'SITES_BACKUP': config.backup_dir,
        **({'SITES_BREW_PREFIX': runtime.brew_prefix} if runtime.brew_prefix else {}),
    }.items()}
    save()
    for name in ('sites', 'newsite', 'sites_platform.py', 'sites_config.py'):
        write_owned(app / 'bin' / name, (source / 'bin' / name).read_bytes(), 0o755 if '.' not in name else 0o644)
    bindir = Path(os.environ.get('SITES_BIN', str(app.parents[1] / 'bin')))
    for name in ('sites', 'newsite'):
        destination = bindir / name
        if destination.exists() and not destination.is_symlink() and str(destination) not in files:
            previous = destination.read_text()
            legacy = (name == 'sites' and all(token in previous for token in ('typer.Typer(', 'def cli_list(', 'def cli_create(')))
            legacy = legacy or (name == 'newsite' and previous.startswith('#!/usr/bin/env bash')
                                and 'SITES_WEBROOT' in previous and 'nginx' in previous and 'mysql' in previous)
            legacy = legacy or (name == 'newsite' and 'def render_nginx(' in previous and 'from sites_config import' in previous)
            if legacy:
                backup = app / 'legacy' / name
                write_owned(backup, destination.read_bytes(), 0o755)
                metadata.setdefault('legacy_files', {})[str(destination)] = str(backup)
                files.pop(str(backup), None)
                files[str(destination)] = digest(destination.read_bytes())
                save()
        wrapper = '#!/bin/sh\nexport PATH=' + shlex.quote(str(app / 'venv/bin')) + ':"$PATH"\nexec ' + shlex.quote(str(app / 'venv/bin/python')) + ' ' + shlex.quote(str(app / 'bin' / name)) + ' "$@"\n'
        write_owned(bindir / name, wrapper, 0o755)
    write_owned(app / 'uninstall.sh', (source / 'uninstall.sh').read_bytes(), 0o755)

    if platform_name == 'macos':
        mailpit_env = Path(os.environ.get('HOMEBREW_USER_CONFIG_HOME', str(Path.home() / '.homebrew'))) / 'services/mailpit.env'
        mailpit_settings = 'MP_SMTP_BIND_ADDR=127.0.0.1:1025\nMP_UI_BIND_ADDR=127.0.0.1:8025\n'
        if mailpit_env.exists() and str(mailpit_env) not in files:
            existing = dict(line.split('=', 1) for line in mailpit_env.read_text().splitlines()
                            if line and not line.startswith('#') and '=' in line)
            if (existing.get('MP_SMTP_BIND_ADDR'), existing.get('MP_UI_BIND_ADDR')) != ('127.0.0.1:1025', '127.0.0.1:8025'):
                raise RuntimeError(f'Preserving {mailpit_env}: configure both Mailpit bindings to loopback before retrying.')
        else:
            mailpit_changed = write_owned(mailpit_env, mailpit_settings, 0o600)
            if mailpit_changed and 'mailpit' in services and manager.status('mailpit') == 'active':
                check(manager.restart('mailpit'))
        write_owned(app / 'packaging/macos/com.sites-dashboard.nginx.plist.in',
                    (source / 'packaging/macos/com.sites-dashboard.nginx.plist.in').read_bytes())
        nginx_dir = runtime.brew_prefix / 'etc/nginx'
        nginx_main = nginx_dir / 'nginx.conf'
        if not nginx_main.exists():
            write_owned(nginx_main, 'events {}\nhttp { include mime.types; include servers/*; }\n')
        main_text = re.sub(r'#[^\n]*', '', nginx_main.read_text())
        if not re.search(r'\binclude\s+["\']?(?:' + re.escape(str(nginx_dir)) + r'/)?servers/\*["\']?\s*;', main_text):
            raise RuntimeError(f'Preserving {nginx_main}: add "include servers/*;" inside its http block, then retry.')
        if re.search(r'(?m)^\s*(user|daemon)\s+', main_text):
            raise RuntimeError(f'{nginx_main}: remove active user/daemon directives; launchd supplies them.')
        runtime.nginx_available.mkdir(parents=True, exist_ok=True)
        runtime.nginx_enabled.mkdir(parents=True, exist_ok=True)
        write_owned(nginx_dir / 'servers/sites-dashboard.conf',
                    '# sites-dashboard\ninclude ' + quote_nginx(runtime.nginx_enabled / '*.conf') + ';\n'
                    'server { listen 80; server_name localhost; location = /__sites_dashboard_ready { return 200 "ready"; } }\n',
                    preserve_edits=True)
        pool_changed = False
        if own_pool:
            version = command([runtime.brew_prefix / 'opt' / runtime.php_service / 'bin/php', '-r',
                               'echo PHP_MAJOR_VERSION, ".", PHP_MINOR_VERSION;'])
            if not re.fullmatch(r'[0-9]+\.[0-9]+', version):
                raise RuntimeError('Cannot determine PHP configuration version.')
            pool = runtime.brew_prefix / f'etc/php/{version}/php-fpm.d/sites-dashboard.conf'
            metadata['php_pool'] = str(pool)
            runtime.php_socket.parent.mkdir(parents=True, exist_ok=True)
            pool_changed = write_owned(pool,
                '; sites-dashboard\n[sites_dashboard]\nlisten = ' + quote_nginx(runtime.php_socket) + '\n'
                'listen.mode = 0600\npm = ondemand\npm.max_children = 5\npm.process_idle_timeout = 10s\n',
                preserve_edits=True)
        command(['sudo', '-v'])
    else:
        arch = command(['uname', '-m'])
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(arch)
        if not arch:
            raise RuntimeError('Unsupported Linux architecture for mkcert/Mailpit downloads.')
        with tempfile.TemporaryDirectory(prefix='sites-download-') as temporary:
            temporary = Path(temporary)
            for name, url in (
                ('mkcert', f'https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-{arch}'),
                ('mailpit', f'https://github.com/axllent/mailpit/releases/download/v1.30.7/mailpit-linux-{arch}.tar.gz'),
            ):
                if shutil.which(name):
                    continue
                download = temporary / (name + '.download')
                command(['curl', '-fLsS', '-o', download, url])
                if name == 'mailpit':
                    command(['tar', 'xzf', download, '-C', temporary])
                    download = temporary / name
                command(['sudo', '-n', 'install', '-m', '755', download, '/usr/local/bin/' + name])
        msmtp = Path('/etc/msmtprc')
        if not msmtp.exists():
            with tempfile.NamedTemporaryFile(mode='w') as f:
                f.write('defaults\nauth off\ntls off\naccount local\nhost 127.0.0.1\nport 1025\nfrom noreply@localhost\naccount default : local\n')
                f.flush()
                command(['sudo', '-n', 'install', '-m', '644', f.name, msmtp])
        unit = Path('/etc/systemd/system/mailpit.service')
        if not unit.exists():
            text = '# sites-dashboard\n[Unit]\nDescription=Mailpit local mail server\nAfter=network.target\n[Service]\nExecStart=/usr/local/bin/mailpit --smtp 127.0.0.1:1025 --listen 127.0.0.1:8025\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n'
            with tempfile.NamedTemporaryFile(mode='w') as f:
                f.write(text)
                f.flush()
                command(['sudo', '-n', 'install', '-m', '644', f.name, unit])
            metadata['mailpit_unit'] = digest(unit.read_bytes())
            save()
            command(['sudo', '-n', 'systemctl', 'daemon-reload'])

    for service in (runtime.php_service, 'mariadb', 'mailpit'):
        active = manager.status(service) == 'active'
        if not active:
            if service not in services:
                services.append(service)
                save()
            check(manager.start(service))
        elif platform_name == 'macos' and service == runtime.php_service and pool_changed:
            check(manager.restart(service))
    wait_ready('PHP-FPM', php_ready)
    wait_ready('MariaDB', lambda: tcp(3306))
    wait_ready('Mailpit SMTP', lambda: tcp(1025))
    wait_ready('Mailpit HTTP', lambda: tcp(8025))
    if platform_name == 'macos':
        def mailpit_loopback():
            listeners = command(['lsof', '-nP', '-a', '-c', 'mailpit', '-iTCP:1025', '-iTCP:8025',
                                 '-sTCP:LISTEN', '-Fn'])
            addresses = [line[1:] for line in listeners.splitlines() if line.startswith('n')]
            if any(address not in ('127.0.0.1:1025', '127.0.0.1:8025') for address in addresses):
                raise ValueError('Mailpit listeners must bind only to loopback (127.0.0.1).')
            if set(addresses) != {'127.0.0.1:1025', '127.0.0.1:8025'}:
                raise RuntimeError('Cannot verify both Mailpit loopback listeners.')
        try:
            wait_ready('Mailpit loopback bindings', mailpit_loopback)
        except (OSError, RuntimeError, ValueError):
            if 'mailpit' in services:
                check(manager.stop('mailpit'))
            raise
        import sites_platform
        sites_platform.NGINX_PLIST_TEMPLATE = app / 'packaging/macos/com.sites-dashboard.nginx.plist.in'
        metadata['nginx_owned'] = True
        save()
        check(install_nginx_service(config, nginx_main, state / 'logs'))
    else:
        if manager.status('nginx') != 'active' and 'nginx' not in services:
            services.append('nginx')
            save()
        check(manager.start('nginx'))
    wait_ready('Nginx (port 80)', lambda: tcp(80))

    begin, end = '# >>> sites-dashboard >>>', '# <<< sites-dashboard <<<'
    lines = [begin]
    for directory in [bindir, *([runtime.brew_prefix / 'bin', runtime.brew_prefix / 'sbin'] if runtime.brew_prefix else [])]:
        quoted = shlex.quote(str(directory))
        lines.append('case ":$PATH:" in *:' + quoted + ':*) ;; *) export PATH=' + quoted + ':"$PATH" ;; esac')
    for key, value in metadata['environment'].items():
        lines.append('[ -n "${' + key + ':-}" ] || export ' + key + '=' + shlex.quote(value))
    lines.append(end)
    block = '\n'.join(lines) + '\n'
    profiles = ('.zprofile', '.zshrc') if platform_name == 'macos' else ('.bashrc', '.zshrc', '.profile')
    for name in profiles:
        path = Path.home() / name
        if path.is_symlink():
            raise RuntimeError(f'Profile is a symlink; add the exports manually: {path}')
        content = path.read_text() if path.exists() else ''
        if begin in content or end in content:
            if content.count(begin) != 1 or content.count(end) != 1 or content.index(end) < content.index(begin):
                raise RuntimeError(f'Malformed sites-dashboard profile block: {path}')
            pattern = re.escape(begin) + r'.*?' + re.escape(end) + r'\n?'
            existing_block = re.search(pattern, content, flags=re.S).group()
            previous_block = metadata.get('profile_blocks', {}).get(str(path))
            if previous_block is not None and existing_block != previous_block:
                print(f'Preserving edited profile block: {path}')
                continue
            updated = re.sub(pattern, lambda _: block, content, flags=re.S)
        else:
            updated = content + ('' if not content or content.endswith('\n') else '\n') + block
        if updated != content:
            path.write_text(updated)
        metadata.setdefault('profiles', [])
        if str(path) not in metadata['profiles']:
            metadata['profiles'].append(str(path))
        metadata.setdefault('profile_blocks', {})[str(path)] = block
    metadata['ready'] = True
    save()
    print(f'Done! Binaries: {bindir}. Open a new shell and run sites dashboard.')
except (OSError, ValueError, RuntimeError) as error:
    print(f'Installation failed: {error}', file=sys.stderr)
    sys.exit(1)
PY
