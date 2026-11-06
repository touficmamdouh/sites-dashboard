"""Exercise lifecycle scripts with real temporary files and fake OS commands."""

import hashlib
import json
import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUN = subprocess.run

FAKE_COMMAND = r'''#!PYTHON
import json, os, shutil, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['SITES_TEST_ROOT'])
brew = root / 'brew custom'
with (root / 'commands.jsonl').open('a') as f:
    f.write(json.dumps([name, *args]) + '\n')
def owned(path):
    p = Path(path)
    assert p.is_absolute() and p.is_relative_to(root), p
    return p
statefile = root / 'services.json'
state = json.loads(statefile.read_text()) if statefile.exists() else {}
config_base = os.environ.get('XDG_CONFIG_HOME') or os.environ.get('HOMEBREW_XDG_CONFIG_HOME')
brew_config = Path(config_base) / 'homebrew' if config_base else Path(os.environ['HOME']) / '.homebrew'
if name == 'uname': print(os.environ.get('MOCK_ARCH', 'arm64') if '-m' in args else os.environ.get('MOCK_OS', 'Darwin'))
elif name == 'sw_vers': print(os.environ.get('MOCK_VERSION', '13.0'))
elif name == 'id': print('1000')
elif name == 'brew':
    if args == ['--prefix']: print(brew)
    elif args == ['ruby', '-e', 'puts ENV.fetch("HOMEBREW_USER_CONFIG_HOME")']: print(brew_config)
    elif args[:2] == ['list', '--versions']:
        if args[-1] not in os.environ.get('MOCK_MISSING_FORMULAE', '').split(','): print(args[-1], '1.0')
        else: sys.exit(1)
    elif args[:2] == ['services', 'list']:
        print(json.dumps([{'name': n, 'status': state.get(n, 'none')} for n in ('php', 'php@8.3', 'mariadb', 'mailpit')]))
    elif args[:2] == ['services', 'info']:
        print(json.dumps([{'name': args[2], 'status': state.get(args[2], 'none')}]))
    elif args[:1] == ['services'] and args[1] in ('start', 'restart', 'stop'):
        if args[1:] == ['restart', 'php'] and os.environ.get('MOCK_PHP_RESTART_FAIL'):
            print('PHP restart failed', file=sys.stderr); sys.exit(1)
        if args[2] == 'mailpit' and args[1] != 'stop':
            envfile = brew_config / 'services/mailpit.env'
            values = dict(line.split('=', 1) for line in envfile.read_text().splitlines()
                          if line and not line.startswith('#')) if envfile.exists() else {}
            (root / 'mailpit-bindings.json').write_text(json.dumps([
                values.get('MP_SMTP_BIND_ADDR', '0.0.0.0:1025'), values.get('MP_UI_BIND_ADDR', '0.0.0.0:8025')]))
        state[args[2]] = 'none' if args[1] == 'stop' else 'started'
        statefile.write_text(json.dumps(state))
    elif args[:1] == ['install']: pass
    else: raise AssertionError(args)
elif name in ('python3', 'python'):
    if args[:2] == ['-m', 'venv']:
        dest = owned(args[2]); (dest / 'bin').mkdir(parents=True, exist_ok=True)
        shutil.copy2(sys.argv[0], dest / 'bin/python')
        shutil.copy2(sys.argv[0], dest / 'bin/python3')
    elif args[:2] == ['-m', 'pip']: pass
    else: os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], *args])
elif name == 'php': print('8.3' if '-r' in args else 'PHP 8.3')
elif name == 'php-fpm': print('[www]\nlisten = ' + str(brew / 'var/run/php-fpm.sock'), file=sys.stderr)
elif name == 'sudo':
    if args == ['-v']:
        (root / 'sudo-valid').touch(); sys.exit(0)
    if os.environ.get('MOCK_SUDO_EXPIRED') and not (root / 'sudo-valid').exists():
        print('sudo: a password is required', file=sys.stderr); sys.exit(1)
    assert args.pop(0) == '-n', args
    command = Path(args.pop(0)).name
    if command in ('install', 'ln', 'mv'):
        source, destination = owned(args[-2]), owned(args[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if command == 'install': shutil.copyfile(source, destination)
        elif command == 'ln': os.link(source, destination)
        else: source.replace(destination)
    elif command == 'rm':
        for path in args:
            if not path.startswith('-'):
                if path == '/etc/systemd/system/mailpit.service':
                    path = root / 'system/etc/systemd/system/mailpit.service'
                owned(path).unlink(missing_ok=True)
    elif command == 'launchctl':
        if args[0] == 'bootstrap': (root / 'loaded').touch()
        elif args[0] == 'bootout': (root / 'loaded').unlink(missing_ok=True)
        elif args[0] not in ('enable', 'disable', 'kickstart'): raise AssertionError(args)
    elif command == 'lsof': sys.exit(1)
    elif command in ('nginx', 'systemctl', 'apt-get'): pass
    else: raise AssertionError([command, *args])
elif name == 'launchctl':
    assert args[:1] == ['print'], args
    if not (root / 'loaded').exists():
        print('Could not find service', file=sys.stderr); sys.exit(113)
    print('path = ' + str(root / 'com.sites-dashboard.nginx.plist') + '\nstate = running\npid = 123')
elif name == 'curl':
    if 'raw.githubusercontent.com/Homebrew/install/HEAD/install.sh' in ' '.join(args):
        print('echo homebrew-installed >> "$SITES_TEST_ROOT/bootstrap"')
    elif any('github.com/axllent/mailpit/releases/' in arg for arg in args):
        import hashlib, io, tarfile
        target = owned(args[args.index('--output') + 1])
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            payload = b'#!/bin/sh\nexit 0\n'
            entry = tarfile.TarInfo('mailpit'); entry.size = len(payload); entry.mode = 0o755
            archive.addfile(entry, io.BytesIO(payload))
        if args[-1].endswith('checksums.txt'):
            target.write_text(hashlib.sha256(data.getvalue()).hexdigest() + '  mailpit-linux-amd64.tar.gz\n')
        else:
            target.write_bytes(data.getvalue())
elif name == 'lsof':
    path = root / 'mailpit-bindings.json'
    addresses = json.loads(path.read_text()) if path.exists() else ['127.0.0.1:1025', '127.0.0.1:8025']
    if os.environ.get('MOCK_MAILPIT_PUBLIC'): addresses = ['*:1025', '*:8025']
    print('p456\n' + '\n'.join('n' + addr for addr in addresses))
elif name == 'sha256sum':
    assert owned(args[0]).is_file()
    checksum = '0' * 64 if os.environ.get('MOCK_BAD_MAILPIT_DOWNLOAD') else 'a35c053509441b95fb96da6479b3a26b3eb309e223232aced28255c011d29efc'
    print(checksum, args[0])
elif name == 'systemctl':
    if 'list-unit-files' in args: print('php8.3-fpm.service enabled')
    elif 'list-units' in args: print('php8.3-fpm.service loaded active running')
    elif 'is-active' in args: print('active')
elif name == 'rm':
    for arg in args:
        if not arg.startswith('-'):
            target = Path(arg)
            if not target.is_absolute(): target = Path.cwd() / target
            target = owned(target)
            if target.is_dir(): shutil.rmtree(target)
            else: target.unlink(missing_ok=True)
elif name in ('nginx', 'mariadb', 'mysql', 'mailpit', 'mkcert', 'wget', 'apt-get', 'pip3', 'msmtp'): pass
else: raise AssertionError([name, *args])
'''

PATCH_HOST = r'''
import os, platform, socket, sys, time
from dataclasses import replace
from pathlib import Path
import tempfile
original_replace = Path.replace
def replace_path(self, destination):
    fault = os.environ.get('MOCK_FINAL_CHECKPOINT_FAULT')
    if fault and str(destination).endswith('/.config/sites-dashboard/runtime.json'):
        data = json.loads(self.read_text())
        complete = os.environ.get('MOCK_CHECKPOINT_KIND') == 'complete'
        if data.get('uninstalled') and (not complete or data.get('cleanup_pending') is not True):
            if fault == 'after':
                original_replace(self, destination)
                os._exit(78)
            if fault == 'before':
                raise OSError('injected checkpoint publication failure')
    if os.environ.get('MOCK_REPLACE_TARGET') == str(destination):
        if os.environ.get('MOCK_REPLACE_PHASE') == 'after':
            original_replace(self, destination)
            os._exit(77)
        raise OSError('injected publication failure')
    return original_replace(self, destination)
Path.replace = replace_path
import json, stat
original_fsync = os.fsync
def fsync(fd):
    if os.environ.get('MOCK_FINAL_CHECKPOINT_FAULT') == 'directory' and stat.S_ISDIR(os.fstat(fd).st_mode):
        path = Path(os.environ['HOME']) / '.config/sites-dashboard/runtime.json'
        if path.exists() and json.loads(path.read_text()).get('uninstalled'):
            raise OSError('injected checkpoint directory sync failure')
    return original_fsync(fd)
os.fsync = fsync
original_unlink = Path.unlink
def unlink_path(self, *args, **kwargs):
    if os.environ.get('MOCK_UNLINK_TARGET') == str(self):
        raise OSError('injected final cleanup failure')
    return original_unlink(self, *args, **kwargs)
Path.unlink = unlink_path
if os.environ.get('MOCK_SYSTEM_ROOT'):
    original_new = Path.__new__
    original_init = Path.__init__
    def redirect_parts(parts):
        if parts and str(parts[0]).startswith(('/etc/', '/var/lib/sites-dashboard', '/usr/lib/systemd/', '/usr/lib/sites-dashboard', '/usr/share/sites-dashboard', '/run/systemd/')):
            return (os.environ['MOCK_SYSTEM_ROOT'] + str(parts[0]), *parts[1:])
        return parts
    def redirect(cls, *parts, **kwargs):
        return original_new(cls, *redirect_parts(parts), **kwargs)
    def redirect_init(self, *parts, **kwargs):
        return original_init(self, *redirect_parts(parts), **kwargs)
    Path.__new__ = staticmethod(redirect)
    Path.__init__ = redirect_init
original_exists = Path.exists
def exists(self):
    if os.environ.get('MOCK_OS') == 'Linux' and str(self) in ('/etc/msmtprc', '/etc/systemd/system/mailpit.service'):
        return True
    return original_exists(self)
Path.exists = exists
platform.system = lambda: os.environ.get('MOCK_OS', 'Darwin')
platform.machine = lambda: os.environ.get('MOCK_ARCH', 'arm64')
time.sleep = lambda _: None
def connect(self, address):
    if os.environ.get('MOCK_NOT_READY') in ('all', str(address)):
        raise OSError('connection refused')
socket.socket.connect = connect
class FakeSocket:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, timeout): pass
    def connect(self, address): connect(self, address)
socket.socket = FakeSocket
def create_connection(address, timeout=None, **kwargs):
    connect(None, address)
    return socket.socket()
socket.create_connection = create_connection
if not os.environ.get('MOCK_INSTALLED_ONLY'):
    sys.path.insert(0, os.environ['SITES_TEST_SOURCE'] + '/bin')
    import sites_platform
    original = sites_platform.detect_runtime
    def detect(*args, **kwargs):
        runtime = original(*args, **kwargs)
        if runtime.platform == 'macos':
            runtime = replace(runtime, nginx_launchd_plist=Path(os.environ['SITES_TEST_ROOT']) / 'com.sites-dashboard.nginx.plist')
        return runtime
    sites_platform.detect_runtime = detect
'''


@pytest.fixture
def sandbox(tmp_path):
    class Sandbox:
        def __init__(self):
            self.root = tmp_path
            self.home = tmp_path / 'home alice'
            self.home.mkdir()
            self.bin = tmp_path / 'commands'
            self.bin.mkdir()
            self.brew = tmp_path / 'brew custom'
            (self.brew / 'bin').mkdir(parents=True)
            self.source = tmp_path / 'source'
            self.source.mkdir()
            for item in ('bin', 'DEBIAN', 'packaging'):
                shutil.copytree(ROOT / item, self.source / item)
            for item in ('install.sh', 'build.sh', 'uninstall.sh'):
                if (ROOT / item).exists():
                    shutil.copy2(ROOT / item, self.source / item)
            patch = tmp_path / 'patch'
            patch.mkdir()
            (patch / 'sitecustomize.py').write_text(PATCH_HOST)
            self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin),
                        'SITES_TEST_ROOT': str(tmp_path), 'SITES_TEST_SOURCE': str(self.source),
                        'SITES_BREW_PREFIX': str(self.brew), 'REAL_PYTHON': sys.executable,
                        'PYTHONPATH': str(patch), 'TMPDIR': str(tmp_path), 'USER': 'alice'}
            for name in ('XDG_CONFIG_HOME', 'HOMEBREW_XDG_CONFIG_HOME', 'HOMEBREW_USER_CONFIG_HOME'):
                self.env.pop(name, None)
            for name in ('bash', 'dirname', 'mkdir', 'install', 'cp', 'chmod', 'rm', 'cat',
                         'grep', 'sed', 'head', 'basename', 'tee', 'tar', 'cut', 'getent',
                         'logname', 'mktemp', 'ln', 'mv', 'ls', 'dpkg-deb', 'sleep'):
                if shutil.which(name):
                    (self.bin / name).symlink_to(shutil.which(name))
            names = ('uname', 'sw_vers', 'id', 'brew', 'python3', 'sudo', 'launchctl',
                     'curl', 'systemctl', 'nginx', 'mariadb', 'mysql', 'mailpit', 'mkcert',
                     'wget', 'apt-get', 'pip3', 'msmtp', 'php', 'php-fpm', 'rm', 'lsof', 'sha256sum')
            for name in names:
                (self.bin / name).unlink(missing_ok=True)
                self.fake(self.bin / name)
            for name in ('brew', 'python3', 'nginx', 'mariadb', 'mailpit', 'mkcert', 'curl', 'wget', 'php'):
                self.fake(self.brew / 'bin' / name)
            self.fake(self.brew / 'opt/php/sbin/php-fpm')
            self.fake(self.brew / 'opt/php@8.3/sbin/php-fpm')
            self.fake(self.brew / 'opt/php/bin/php')
            self.fake(self.brew / 'opt/php@8.3/bin/php')
            config = self.brew / 'etc/nginx/nginx.conf'
            config.parent.mkdir(parents=True)
            config.write_text('# user edits\nevents {}\nhttp { include servers/*; }\n')
            (self.brew / 'etc/php/8.3/php-fpm.d').mkdir(parents=True)

        def fake(self, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE_COMMAND.replace('PYTHON', sys.executable + ' -S', 1))
            path.chmod(0o755)

        def run(self, script='install.sh', *args, interactive=False):
            command = [str(self.bin / 'bash'), str(self.source / script), *args]
            env = {**self.env, **({'MOCK_INSTALLED_ONLY': '1'} if script == 'uninstall.sh' else {})}
            if not interactive:
                return RUN(command, env=env, cwd=self.source, text=True, input='', capture_output=True, timeout=30)
            master, slave = pty.openpty()
            process = subprocess.Popen(command, env=self.env, stdin=slave, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            os.close(slave)
            os.write(master, b'n\n')
            try:
                stdout, _ = process.communicate(timeout=30)
                return subprocess.CompletedProcess(command, process.returncode, stdout, '')
            finally:
                os.close(master)

        def calls(self):
            path = tmp_path / 'commands.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        @property
        def state(self):
            return self.home / '.config/sites-dashboard'

        def missing_brew(self):
            (self.bin / 'brew').unlink()
            (self.brew / 'bin/brew').unlink()

    return Sandbox()


def installed(sandbox):
    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((sandbox.state / 'runtime.json').read_text())


def test_macos_installer_requires_consent_for_missing_brew(sandbox):
    sandbox.missing_brew()
    result = sandbox.run(interactive=True)
    assert result.returncode != 0
    assert 'Homebrew' in result.stdout and '[y/N]' in result.stdout
    assert not any(c[0] == 'curl' for c in sandbox.calls())
    assert not (sandbox.root / 'bootstrap').exists()


def test_noninteractive_missing_brew_prints_manual_steps(sandbox):
    sandbox.missing_brew()
    result = sandbox.run()
    assert result.returncode != 0
    assert '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' in result.stdout + result.stderr
    assert 'shellenv' in result.stdout + result.stderr
    assert not any(c[0] == 'curl' for c in sandbox.calls())


def test_profile_update_is_idempotent(sandbox):
    original = '# my setup\nexport SITES_WEBROOT="$HOME/my sites"\n'
    for name in ('.zprofile', '.zshrc'):
        (sandbox.home / name).write_text(original)
    installed(sandbox)
    before = {name: (sandbox.home / name).read_bytes() for name in ('.zprofile', '.zshrc')}
    installed(sandbox)
    for name, content in before.items():
        assert (sandbox.home / name).read_bytes() == content
        assert content.startswith(original.encode())
        assert content.count(b'# >>> sites-dashboard >>>') == 1
    assert not any('apt-get' in c or 'systemctl' in c for c in sandbox.calls())


def test_upgrade_preserves_runtime_state(sandbox):
    installed(sandbox)
    runtime = sandbox.state / 'runtime.json'
    data = json.loads(runtime.read_text())
    data['user_note'] = 'keep me'
    runtime.write_text(json.dumps(data))
    files = [sandbox.home / 'www/example.test/index.php', sandbox.home / 'backups/snapshot.sql',
             sandbox.home / '.local/share/mkcert/rootCA.pem', sandbox.brew / 'var/mysql/database.frm',
             sandbox.brew / 'etc/nginx/sites-available/example.test.conf']
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('user data')
    installed(sandbox)
    assert json.loads(runtime.read_text())['user_note'] == 'keep me'
    assert all(path.read_text() == 'user data' for path in files)
    assert (sandbox.brew / 'etc/nginx/nginx.conf').read_text().startswith('# user edits')
    assert sum(c[:3] == ['sudo', '-n', 'launchctl'] and 'bootstrap' in c for c in sandbox.calls()) == 1


def test_deb_contains_shared_modules(sandbox):
    result = sandbox.run('build.sh', '1.3.0')
    assert result.returncode == 0, result.stdout + result.stderr
    package = sandbox.source / 'sites-dashboard_1.3.0_all.deb'
    listing = RUN(['dpkg-deb', '-c', str(package)], capture_output=True, text=True, check=True).stdout
    assert listing.splitlines()[0].startswith('drwxr-xr-x root/root')
    for name in ('sites', 'newsite', 'sites_platform.py', 'sites_config.py'):
        assert './usr/local/bin/' + name in listing
    assert 'macos' not in listing and 'launchd' not in listing


def test_uninstall_preserves_sites_backups_and_certificates(sandbox):
    installed(sandbox)
    paths = [sandbox.home / 'www/example/index.php', sandbox.home / 'backups/save.sql',
             sandbox.home / '.local/share/mkcert/rootCA.pem', sandbox.brew / 'var/mysql/users.frm']
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('precious')
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(path.read_text() == 'precious' for path in paths)
    assert not (sandbox.home / '.local/bin/sites').exists()
    assert not (sandbox.root / 'com.sites-dashboard.nginx.plist').exists()
    assert '# >>> sites-dashboard >>>' not in (sandbox.home / '.zshrc').read_text()
    assert not any('uninstall' in c or 'purge' in c for c in sandbox.calls())


@pytest.mark.parametrize('version,arch', [('12.7', 'arm64'), ('13.0', 'unknown')])
def test_unsupported_macos_fails_before_dependency_mutation(sandbox, version, arch):
    sandbox.env.update(MOCK_VERSION=version, MOCK_ARCH=arch)
    result = sandbox.run()
    assert result.returncode != 0
    assert not any(c[0] in ('brew', 'apt-get', 'sudo') for c in sandbox.calls())


def test_readiness_failure_does_not_report_success(sandbox):
    sandbox.env['MOCK_NOT_READY'] = 'all'
    result = sandbox.run()
    assert result.returncode != 0
    assert 'ready' in (result.stdout + result.stderr).lower()
    assert 'Done!' not in result.stdout


def test_macos_uses_application_venv_and_all_required_formulae(sandbox):
    sandbox.env['MOCK_MISSING_FORMULAE'] = 'python,nginx,php,mariadb,mailpit,mkcert,curl,wget'
    installed(sandbox)
    calls = sandbox.calls()
    formulae = {arg for c in calls if c[:2] == ['brew', 'install'] for arg in c[2:]}
    assert formulae == {'python', 'nginx', 'php', 'mariadb', 'mailpit', 'mkcert', 'curl', 'wget'}
    assert any(c[:3] == ['python3', '-m', 'venv'] and str(sandbox.home) in c[3] for c in calls)
    assert not any('sudo' in c and any('brew' in arg for arg in c[2:]) for c in calls if 'services' in c)
    assert (sandbox.home / '.local/bin/sites').is_file()
    assert (sandbox.home / '.local/bin/newsite').is_file()
    result = RUN([str(sandbox.home / '.local/bin/newsite'), '--help'], env=sandbox.env,
                 text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_uninstall_leaves_preexisting_brew_services_running(sandbox):
    (sandbox.root / 'services.json').write_text(json.dumps({'mariadb': 'started', 'mailpit': 'started'}))
    installed(sandbox)
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads((sandbox.root / 'services.json').read_text())
    assert state['mariadb'] == state['mailpit'] == 'started'
    assert not any('uninstall' in c for c in sandbox.calls())


def test_debian_prerm_does_not_remove_binaries_on_upgrade_or_purge_dependencies(sandbox):
    for reason in ('upgrade', 'remove', 'failed-upgrade', 'deconfigure'):
        result = sandbox.run('DEBIAN/prerm', reason)
        assert result.returncode == 0, result.stderr
    assert not any(c[0] in ('apt-get', 'pip3', 'systemctl', 'rm') for c in sandbox.calls())


def test_uninstall_is_idempotent(sandbox):
    installed(sandbox)
    first = sandbox.run('uninstall.sh')
    assert first.returncode == 0, first.stderr
    second = sandbox.run('uninstall.sh')
    assert second.returncode == 0, second.stderr


def test_upgrade_selects_active_php_service(sandbox):
    (sandbox.root / 'services.json').write_text(json.dumps({'php@8.3': 'started'}))
    data = installed(sandbox)
    assert data['runtime']['php_service'] == 'php@8.3'


def test_deb_declares_ui_dependencies(sandbox):
    result = sandbox.run('build.sh', '1.3.0')
    assert result.returncode == 0, result.stderr
    deps = RUN(['dpkg-deb', '-f', str(sandbox.source / 'sites-dashboard_1.3.0_all.deb'), 'Depends'],
               capture_output=True, text=True, check=True).stdout
    for name in ('python3-typer', 'python3-rich', 'python3-textual'):
        assert name in deps


def test_linux_installer_keeps_apt_and_systemd_isolated(sandbox):
    sandbox.env.update(MOCK_OS='Linux', MOCK_ARCH='x86_64')
    installed(sandbox)
    calls = sandbox.calls()
    assert any('apt-get' in c and 'install' in c for c in calls)
    assert any('systemctl' in c and 'enable' in c for c in calls)
    assert not any(c[0] in ('brew', 'launchctl') for c in calls)
    assert (sandbox.home / '.bashrc').is_file()


def test_uninstall_preserves_edited_owned_config_and_profile_text(sandbox):
    installed(sandbox)
    owned = sandbox.brew / 'etc/nginx/servers/sites-dashboard.conf'
    owned.write_text(owned.read_text() + '# manual edit\n')
    profile = sandbox.home / '.zshrc'
    profile.write_text('# keep before\n' + profile.read_text() + '# keep after\n')
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stderr
    assert owned.read_text().endswith('# manual edit\n')
    assert profile.read_text() == '# keep before\n# keep after\n'


@pytest.mark.parametrize('address', ["('127.0.0.1', 3306)", "('127.0.0.1', 1025)",
                                   "('127.0.0.1', 8025)", "('127.0.0.1', 80)"])
def test_every_tcp_service_must_be_reachable(sandbox, address):
    sandbox.env['MOCK_NOT_READY'] = address
    result = sandbox.run()
    assert result.returncode != 0
    assert 'not ready' in result.stderr
    assert not json.loads((sandbox.state / 'runtime.json').read_text())['ready']


def test_delete_data_flag_rejects_home_directory_before_removing_install(sandbox):
    sandbox.env['SITES_WEBROOT'] = str(sandbox.home)
    installed(sandbox)
    result = sandbox.run('uninstall.sh', '--delete-data')
    assert result.returncode != 0
    assert (sandbox.home / '.local/bin/sites').exists()


def test_delete_data_flag_removes_only_configured_sites_and_backups(sandbox):
    installed(sandbox)
    for directory in ('www/site', 'backups'):
        target = sandbox.home / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / 'data').touch()
    result = sandbox.run('uninstall.sh', '--delete-data')
    assert result.returncode == 0, result.stderr
    assert not (sandbox.home / 'www').exists()
    assert not (sandbox.home / 'backups').exists()


def test_upgrade_recognizes_legacy_installed_cli_without_losing_original(sandbox):
    legacy = sandbox.home / '.local/bin/sites'
    legacy.parent.mkdir(parents=True)
    content = '#!/usr/bin/env python3\napp = typer.Typer()\ndef cli_list(): pass\ndef cli_create(): pass\n'
    legacy.write_text(content)
    data = installed(sandbox)
    assert legacy.read_text() != content
    assert Path(data['legacy_files'][str(legacy)]).read_text() == content


def test_profile_edits_inside_owned_block_survive_upgrade(sandbox):
    installed(sandbox)
    profile = sandbox.home / '.zshrc'
    edited = profile.read_text().replace('# <<< sites-dashboard <<<', 'export MY_SETTING=keep\n# <<< sites-dashboard <<<')
    profile.write_text(edited)
    installed(sandbox)
    assert profile.read_text() == edited
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stderr
    assert 'export MY_SETTING=keep' in profile.read_text()


def test_debian_postinst_does_not_modify_shared_python_packages(sandbox):
    sandbox.env['MOCK_SYSTEM_ROOT'] = str(sandbox.root / 'system')
    sandbox.env.update(MOCK_OS='Linux', MOCK_ARCH='x86_64')
    package = sandbox.run('build.sh', '1.3.0')
    assert package.returncode == 0, package.stderr
    RUN(['dpkg-deb', '-x', str(sandbox.source / 'sites-dashboard_1.3.0_all.deb'),
         str(sandbox.root / 'system')], check=True, capture_output=True)
    result = sandbox.run('DEBIAN/postinst', 'configure')
    assert result.returncode == 0, result.stderr
    assert not any('pip' in c or 'apt-get' in c for c in sandbox.calls())


def test_macos_mailpit_is_loopback_after_service_restart(sandbox):
    installed(sandbox)
    binding_file = sandbox.root / 'mailpit-bindings.json'
    assert json.loads(binding_file.read_text()) == ['127.0.0.1:1025', '127.0.0.1:8025']
    result = RUN([str(sandbox.brew / 'bin/brew'), 'services', 'restart', 'mailpit'],
                 env=sandbox.env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(binding_file.read_text()) == ['127.0.0.1:1025', '127.0.0.1:8025']
    assert any(c[0] == 'lsof' for c in sandbox.calls())


def test_macos_rejects_public_mailpit_and_stops_owned_service(sandbox):
    sandbox.env['MOCK_MAILPIT_PUBLIC'] = '1'
    result = sandbox.run()
    assert result.returncode != 0
    assert 'loopback' in result.stderr.lower()
    assert json.loads((sandbox.root / 'services.json').read_text())['mailpit'] == 'none'


def test_uninstall_php_failure_retains_cleanup_dependencies_and_can_retry(sandbox):
    (sandbox.root / 'services.json').write_text(json.dumps({'php': 'started'}))
    data = installed(sandbox)
    script = Path(data['app']) / 'uninstall.sh'
    sandbox.env['MOCK_PHP_RESTART_FAIL'] = '1'
    result = sandbox.run('uninstall.sh')
    assert result.returncode != 0
    assert script.exists()
    assert (Path(data['app']) / 'bin/sites_platform.py').exists()
    assert not json.loads((sandbox.state / 'runtime.json').read_text())['uninstalled']
    sandbox.env.pop('MOCK_PHP_RESTART_FAIL')
    # Retry the installed uninstaller, not a source copy importing source modules.
    result = RUN([str(sandbox.bin / 'bash'), str(script)],
                 env={**sandbox.env, 'MOCK_INSTALLED_ONLY': '1'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((sandbox.state / 'runtime.json').read_text())['uninstalled']
    assert '# >>> sites-dashboard >>>' not in (sandbox.home / '.zshrc').read_text()


def test_linux_uninstall_authenticates_before_privileged_cleanup(sandbox):
    sandbox.env.update(MOCK_OS='Linux', MOCK_ARCH='x86_64')
    sandbox.env['MOCK_SYSTEM_ROOT'] = str(sandbox.root / 'system')
    data = installed(sandbox)
    unit = sandbox.root / 'system/etc/systemd/system/mailpit.service'
    assert unit.exists()
    (sandbox.root / 'sudo-valid').unlink(missing_ok=True)
    sandbox.env['MOCK_SUDO_EXPIRED'] = '1'
    before = len(sandbox.calls())
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stderr
    calls = [c for c in sandbox.calls()[before:] if c[0] == 'sudo']
    assert calls[0] == ['sudo', '-v']
    assert not unit.exists()


@pytest.mark.parametrize('phase', ['before', 'after'])
@pytest.mark.parametrize('recovery', ['upgrade', 'uninstall'])
def test_interrupted_replacement_accepts_old_and_new_owned_bytes(sandbox, phase, recovery):
    data = installed(sandbox)
    target = Path(data['app']) / 'bin/sites_config.py'
    original = target.read_bytes()
    newer = original + b'\nUPGRADE_MARKER = True\n'
    (sandbox.source / 'bin/sites_config.py').write_bytes(newer)
    sandbox.env.update(MOCK_REPLACE_TARGET=str(target), MOCK_REPLACE_PHASE=phase)
    failed = sandbox.run()
    assert failed.returncode != 0
    assert target.read_bytes() == (original if phase == 'before' else newer)
    sandbox.env.pop('MOCK_REPLACE_TARGET')
    result = sandbox.run('uninstall.sh' if recovery == 'uninstall' else 'install.sh')
    assert result.returncode == 0, result.stderr
    if recovery == 'uninstall':
        assert not target.exists()
    else:
        assert target.read_bytes() == newer
        state = json.loads((sandbox.state / 'runtime.json').read_text())
        assert state['owned_files'][str(target)] == hashlib.sha256(newer).hexdigest()
        assert not state.get('pending_files')


def test_debian_fresh_install_provisions_loopback_mail_and_preserves_user_data(sandbox):
    sandbox.env.update(MOCK_OS='Linux', MOCK_ARCH='x86_64',
                       MOCK_SYSTEM_ROOT=str(sandbox.root / 'system'))
    result = sandbox.run('build.sh', '1.3.0')
    assert result.returncode == 0, result.stderr
    package = sandbox.source / 'sites-dashboard_1.3.0_all.deb'
    destination = sandbox.root / 'system'
    RUN(['dpkg-deb', '-x', str(package), str(destination)], check=True, capture_output=True)
    result = sandbox.run('DEBIAN/postinst', 'configure')
    assert result.returncode == 0, result.stderr
    binary = destination / 'var/lib/sites-dashboard/bin/mailpit'
    unit = destination / 'usr/lib/systemd/system/sites-dashboard-mailpit.service'
    relay = destination / 'etc/msmtprc'
    assert binary.is_file() and os.access(binary, os.X_OK)
    assert '--smtp 127.0.0.1:1025' in unit.read_text()
    assert '--listen 127.0.0.1:8025' in unit.read_text()
    assert 'Alias=mailpit.service' in unit.read_text()
    assert 'host 127.0.0.1\nport 1025' in relay.read_text()
    assert any(c[:3] == ['systemctl', 'enable', '--now'] for c in sandbox.calls())
    original = relay.read_bytes()
    again = sandbox.run('DEBIAN/postinst', 'configure')
    assert again.returncode == 0, again.stderr
    assert relay.read_bytes() == original
    relay.write_text('# user edited relay\n')
    data = destination / 'var/lib/sites-dashboard-mailpit/mailpit.db'
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b'mail and databases must remain')
    removed = sandbox.run('DEBIAN/prerm', 'remove')
    assert removed.returncode == 0, removed.stderr
    assert data.read_bytes() == b'mail and databases must remain'
    assert relay.read_text() == '# user edited relay\n'
    assert not binary.exists()


def test_uninstall_retry_after_modules_are_partly_removed_uses_journal(sandbox):
    data = installed(sandbox)
    app = Path(data['app'])
    sandbox.env['MOCK_UNLINK_TARGET'] = str(app / 'bin/sites_config.py')
    result = sandbox.run('uninstall.sh')
    assert result.returncode != 0
    assert not (app / 'bin/sites_platform.py').exists()
    assert (app / 'uninstall.sh').exists()
    assert json.loads((sandbox.state / 'runtime.json').read_text())['cleanup_runtime_done']
    sandbox.env.pop('MOCK_UNLINK_TARGET')
    result = RUN([str(sandbox.bin / 'bash'), str(app / 'uninstall.sh')],
                 env={**sandbox.env, 'MOCK_INSTALLED_ONLY': '1'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((sandbox.state / 'runtime.json').read_text())['uninstalled']


def test_debian_rejects_download_checksum_mismatch(sandbox):
    sandbox.env.update(MOCK_OS='Linux', MOCK_ARCH='x86_64', MOCK_BAD_MAILPIT_DOWNLOAD='1',
                       MOCK_SYSTEM_ROOT=str(sandbox.root / 'system'))
    result = sandbox.run('DEBIAN/postinst', 'configure')
    assert result.returncode != 0
    assert 'checksum' in result.stderr
    assert not (sandbox.root / 'system/var/lib/sites-dashboard/bin/mailpit').exists()
    assert not any('enable' in c for c in sandbox.calls())


def test_replacement_recovery_preserves_intervening_user_edit(sandbox):
    data = installed(sandbox)
    target = Path(data['app']) / 'bin/sites_config.py'
    updated = target.read_bytes() + b'\nUPGRADED = True\n'
    (sandbox.source / 'bin/sites_config.py').write_bytes(updated)
    sandbox.env.update(MOCK_REPLACE_TARGET=str(target), MOCK_REPLACE_PHASE='before')
    assert sandbox.run().returncode != 0
    sandbox.env.pop('MOCK_REPLACE_TARGET')
    edited = target.read_bytes() + b'\nUSER_EDIT = True\n'
    target.write_bytes(edited)
    assert sandbox.run().returncode != 0
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == edited


@pytest.mark.parametrize('xdg,brew_xdg,relative', [
    ('xdg', None, 'xdg/homebrew'),
    (None, 'brew-xdg', 'brew-xdg/homebrew'),
    ('xdg', 'brew-xdg', 'xdg/homebrew'),
    ('', 'brew-xdg', 'brew-xdg/homebrew'),
    ('', '', 'home alice/.homebrew'),
])
def test_mailpit_uses_homebrew_effective_config_directory(sandbox, xdg, brew_xdg, relative):
    for name, value in [('XDG_CONFIG_HOME', xdg), ('HOMEBREW_XDG_CONFIG_HOME', brew_xdg)]:
        if value is not None:
            sandbox.env[name] = str(sandbox.root / value) if value else ''
    # Homebrew derives this variable itself, ignoring stale inherited values.
    sandbox.env['HOMEBREW_USER_CONFIG_HOME'] = str(sandbox.root / 'stale-config')
    data = installed(sandbox)
    envfile = sandbox.root / relative / 'services/mailpit.env'
    assert envfile.is_file()
    assert str(envfile) in data['owned_files']
    assert not (sandbox.root / 'stale-config/services/mailpit.env').exists()
    result = RUN([str(sandbox.brew / 'bin/brew'), 'services', 'restart', 'mailpit'],
                 env=sandbox.env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((sandbox.root / 'mailpit-bindings.json').read_text()) == ['127.0.0.1:1025', '127.0.0.1:8025']


def test_mailpit_validates_existing_settings_in_effective_config_directory(sandbox):
    sandbox.env['XDG_CONFIG_HOME'] = str(sandbox.root / 'xdg')
    envfile = sandbox.root / 'xdg/homebrew/services/mailpit.env'
    envfile.parent.mkdir(parents=True)
    content = 'MP_SMTP_BIND_ADDR=0.0.0.0:1025\nMP_UI_BIND_ADDR=0.0.0.0:8025\n'
    envfile.write_text(content)
    result = sandbox.run()
    assert result.returncode != 0
    assert envfile.read_text() == content
    assert not any(c[:4] == ['brew', 'services', 'start', 'mailpit'] for c in sandbox.calls())


@pytest.mark.parametrize('kind,phase', [
    ('prepare', 'before'), ('prepare', 'after'), ('prepare', 'directory'),
    ('complete', 'before'), ('complete', 'after'),
])
def test_uninstall_checkpoint_failure_keeps_installed_retry_entrypoint(sandbox, kind, phase):
    data = installed(sandbox)
    app = Path(data['app'])
    sandbox.env.update(MOCK_FINAL_CHECKPOINT_FAULT=phase, MOCK_CHECKPOINT_KIND=kind)
    failed = sandbox.run('uninstall.sh')
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert (app / 'uninstall.sh').is_file()
    if kind == 'prepare':
        assert (app / 'bin/sites_platform.py').is_file()
        assert (app / 'venv/bin/python').is_file()
    sandbox.env.pop('MOCK_FINAL_CHECKPOINT_FAULT')
    result = RUN([str(sandbox.bin / 'bash'), str(app / 'uninstall.sh')],
                 env={**sandbox.env, 'MOCK_INSTALLED_ONLY': '1'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    completed = json.loads((sandbox.state / 'runtime.json').read_text())
    assert completed['uninstalled'] and not completed.get('cleanup_pending')
    assert not (app / 'uninstall.sh').exists()


def test_retry_entrypoint_unlink_failure_has_durable_completion_record(sandbox):
    data = installed(sandbox)
    script = Path(data['app']) / 'uninstall.sh'
    sandbox.env['MOCK_UNLINK_TARGET'] = str(script)
    result = sandbox.run('uninstall.sh')
    assert result.returncode != 0
    assert script.is_file()
    completed = json.loads((sandbox.state / 'runtime.json').read_text())
    assert completed['uninstalled'] and not completed.get('cleanup_pending')
    sandbox.env.pop('MOCK_UNLINK_TARGET')
    result = RUN([str(sandbox.bin / 'bash'), str(script)],
                 env={**sandbox.env, 'MOCK_INSTALLED_ONLY': '1'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not script.exists()


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_interrupted_upgrade_then_uninstall_then_reinstall_retires_journal(sandbox, phase):
    data = installed(sandbox)
    target = Path(data['app']) / 'bin/sites_config.py'
    (sandbox.source / 'bin/sites_config.py').write_bytes(target.read_bytes() + b'\nROUND_TWO = True\n')
    sandbox.env.update(MOCK_REPLACE_TARGET=str(target), MOCK_REPLACE_PHASE=phase)
    assert sandbox.run().returncode != 0
    pending = json.loads((sandbox.state / 'runtime.json').read_text())['pending_files']
    assert str(target) in pending
    staged = Path(pending[str(target)]['temporary'])
    sandbox.env.pop('MOCK_REPLACE_TARGET')
    result = sandbox.run('uninstall.sh')
    assert result.returncode == 0, result.stderr
    assert not target.exists() and not staged.exists()
    removed = json.loads((sandbox.state / 'runtime.json').read_text())
    assert not removed.get('pending_files')
    data = installed(sandbox)
    assert data['ready'] and not data['uninstalled']


@pytest.mark.parametrize('script_state', ['missing', 'missing-pending', 'pending-only', 'unrecorded'])
def test_incomplete_uninstall_without_recorded_retry_script_finishes_cleanly(sandbox, script_state):
    app = sandbox.home / '.local/share/sites-dashboard'
    app.mkdir(parents=True)
    sandbox.state.mkdir(parents=True)
    script = app / 'uninstall.sh'
    content = b'#!/bin/sh\n# preserved incomplete-install fixture\n'
    pending = {}
    staging = app / '.sites-dashboard-incomplete'
    if script_state in ('pending-only', 'unrecorded'):
        script.write_bytes(content)
    if script_state in ('missing-pending', 'pending-only'):
        staging.write_bytes(content)
        pending[str(script)] = {'old': None, 'new': hashlib.sha256(content).hexdigest(),
                                'temporary': str(staging)}
    statefile = sandbox.state / 'runtime.json'
    statefile.write_text(json.dumps({'app': str(app), 'owned_files': {}, 'pending_files': pending,
                                    'cleanup_runtime_done': True, 'uninstalled': False}))
    for _ in range(2):
        result = sandbox.run('uninstall.sh')
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(statefile.read_text())
        assert state['uninstalled'] and not state['cleanup_pending']
        assert not state['pending_files']
    assert not staging.exists()
    if script_state == 'unrecorded':
        assert script.read_bytes() == content
        assert str(script) not in state['owned_files']
    else:
        assert not script.exists()
