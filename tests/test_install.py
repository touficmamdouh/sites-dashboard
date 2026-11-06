"""Exercise lifecycle scripts with real temporary files and fake OS commands."""

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
if name == 'uname': print(os.environ.get('MOCK_ARCH', 'arm64') if '-m' in args else os.environ.get('MOCK_OS', 'Darwin'))
elif name == 'sw_vers': print(os.environ.get('MOCK_VERSION', '13.0'))
elif name == 'id': print('1000')
elif name == 'brew':
    if args == ['--prefix']: print(brew)
    elif args[:2] == ['list', '--versions']:
        if args[-1] not in os.environ.get('MOCK_MISSING_FORMULAE', '').split(','): print(args[-1], '1.0')
        else: sys.exit(1)
    elif args[:2] == ['services', 'list']:
        print(json.dumps([{'name': n, 'status': state.get(n, 'none')} for n in ('php', 'php@8.3', 'mariadb', 'mailpit')]))
    elif args[:2] == ['services', 'info']:
        print(json.dumps([{'name': args[2], 'status': state.get(args[2], 'none')}]))
    elif args[:1] == ['services'] and args[1] in ('start', 'restart', 'stop'):
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
    if args == ['-v']: sys.exit(0)
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
            if not path.startswith('-'): owned(path).unlink(missing_ok=True)
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
            for name in ('bash', 'dirname', 'mkdir', 'install', 'cp', 'chmod', 'rm', 'cat',
                         'grep', 'sed', 'head', 'basename', 'tee', 'tar', 'cut', 'getent',
                         'logname', 'mktemp', 'ln', 'mv', 'ls', 'dpkg-deb', 'sleep'):
                if shutil.which(name):
                    (self.bin / name).symlink_to(shutil.which(name))
            names = ('uname', 'sw_vers', 'id', 'brew', 'python3', 'sudo', 'launchctl',
                     'curl', 'systemctl', 'nginx', 'mariadb', 'mysql', 'mailpit', 'mkcert',
                     'wget', 'apt-get', 'pip3', 'msmtp', 'php', 'php-fpm', 'rm')
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
    assert not sandbox.calls()


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
    result = sandbox.run('DEBIAN/postinst', 'configure')
    assert result.returncode == 0, result.stderr
    assert not any('pip' in c or 'apt-get' in c for c in sandbox.calls())
