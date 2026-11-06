import json
import os
import plistlib
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import sites_platform
from sites_config import load_config
from sites_platform import CommandResult, RuntimePaths, ServiceManager


LABEL = "com.sites-dashboard.nginx"
TARGET = "system/com.sites-dashboard.nginx"
PORTS = ("sudo", "-n", "lsof", "-nP", "+c", "0", "-iTCP:80", "-iTCP:443", "-sTCP:LISTEN", "-Fpcn")


@pytest.fixture(params=["/opt/homebrew", "/usr/local", "/custom/brew"])
def runtime(tmp_path, request):
    prefix = Path(request.param)
    return RuntimePaths(
        "macos", "arm64" if request.param != "/usr/local" else "x86_64", prefix,
        prefix / "etc/nginx/sites-available", prefix / "etc/nginx/sites-enabled",
        "launchd", "php@8.3", prefix / "var/run/php-fpm.sock",
        prefix / "opt/php@8.3/sbin/php-fpm", "alice", "staff", prefix / "bin/brew",
        LABEL, tmp_path / f"{LABEL}.plist",
    )


@pytest.fixture
def paths(tmp_path):
    state = tmp_path / "home alice/.config/sites-dashboard"
    state.mkdir(parents=True)
    config = state / "nginx.conf"
    config.write_text("events {}\nhttp { server { listen 80; } }\n")
    return config, state / "logs"


class ServiceRunner:
    """Emulate commands at the OS boundary, preserving real temporary plist IO."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []
        self.loaded = False
        self.state = "running"
        self.listeners = CommandResult("", "", 1)
        self.processes = CommandResult("123 1 nginx: master process\n124 123 nginx: worker process\n999 1 nginx\n1000 999 nginx\n")
        self.failure = None
        self.before_install = None

    def __call__(self, command):
        cmd = tuple(map(str, command))
        self.calls.append(cmd)
        assert not ("sudo" in cmd and str(self.runtime.brew_binary) in cmd)
        if self.failure and self.failure[0](cmd):
            return self.failure[1]
        if cmd == ("launchctl", "print", TARGET):
            if self.loaded:
                return CommandResult(f"path = {self.runtime.nginx_launchd_plist}\nstate = {self.state}\npid = 123\n")
            return CommandResult("", f'Could not find service "{LABEL}" in domain for system', 113)
        if cmd == PORTS:
            return self.listeners
        if cmd == ("ps", "-axo", "pid=,ppid=,comm="):
            return self.processes
        if cmd[:3] == ("sudo", "-n", "launchctl"):
            assert cmd[3:] in {
                ("enable", TARGET), ("bootstrap", "system", str(self.runtime.nginx_launchd_plist)),
                ("bootout", TARGET), ("kickstart", "-k", TARGET),
            }
            if cmd[3] == "bootstrap":
                assert not self.loaded, "bootstrap must not be repeated for a loaded job"
                self.loaded = True
            elif cmd[3] == "bootout":
                self.loaded = False
            return CommandResult()
        if cmd[:3] == ("sudo", "-n", "install"):
            assert cmd[3:9] == ("-o", "root", "-g", "wheel", "-m", "0644")
            source, staged = map(Path, cmd[-2:])
            assert source.is_file()
            if self.before_install:
                self.before_install()
            shutil.copyfile(source, staged)
            staged.chmod(0o644)
            return CommandResult()
        if cmd[:3] == ("sudo", "-n", "ln"):
            try:
                os.link(cmd[3], cmd[4])
            except FileExistsError:
                return CommandResult("", "File exists", 1)
            return CommandResult()
        if cmd[:4] == ("sudo", "-n", "mv", "-f"):
            Path(cmd[4]).replace(cmd[5])
            return CommandResult()
        if cmd[:4] == ("sudo", "-n", "rm", "-f"):
            Path(cmd[4]).unlink(missing_ok=True)
            return CommandResult()
        if cmd[:3] == ("sudo", "-n", str(self.runtime.brew_prefix / "bin/nginx")):
            assert cmd[3] == "-t" and cmd[4] == "-c" and cmd[6:] == ("-g", "daemon off; user alice staff;")
            return CommandResult()
        pytest.fail(f"Unexpected command: {cmd}")


@pytest.fixture
def runner(runtime):
    return ServiceRunner(runtime)


def install(runtime, paths, runner):
    config = load_config({"HOME": str(paths[0].parent), "USER": "alice"}, runtime)
    return sites_platform.install_nginx_service(config, paths[0], paths[1], runner=runner)


def test_rendered_plist_uses_homebrew_nginx_and_current_user(runtime, paths):
    rendered = sites_platform.render_nginx_launchd_plist(runtime, *paths)
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == "com.sites-dashboard.nginx"
    assert plist["ProgramArguments"] == [
        str(runtime.brew_prefix) + "/bin/nginx", "-c", str(paths[0]),
        "-g", "daemon off; user alice staff;",
    ]
    assert "UserName" not in plist and "GroupName" not in plist
    assert plist["SitesDashboardOwner"] == "alice"
    assert plist["StandardOutPath"] == str(paths[1] / "nginx.stdout.log")
    assert plist["StandardErrorPath"] == str(paths[1] / "nginx.stderr.log")
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True


def test_nginx_service_is_system_launchd_not_sudo_brew_services(runtime, paths, runner):
    result = install(runtime, paths, runner)
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(runtime.nginx_launchd_plist.read_bytes())
    assert plist["Label"] == LABEL
    assert runtime.nginx_launchd_plist.stat().st_mode & 0o777 == 0o644
    assert paths[1].is_dir() and paths[1].stat().st_uid == os.getuid()
    assert ("sudo", "-n", "launchctl", "enable", TARGET) in runner.calls
    assert ("sudo", "-n", "launchctl", "bootstrap", "system", str(runtime.nginx_launchd_plist)) in runner.calls
    assert PORTS in runner.calls
    assert ("sudo", "-n", str(runtime.brew_prefix / "bin/nginx"), "-t", "-c", str(paths[0]),
            "-g", "daemon off; user alice staff;") in runner.calls
    assert not any("brew" in Path(arg).name for call in runner.calls for arg in call)


@pytest.mark.parametrize("service", ["php@8.3", "mariadb", "mailpit"])
def test_brew_services_are_user_scoped_for_php_mariadb_mailpit(runtime, fake_runner, service):
    brew = str(runtime.brew_binary)
    commands = [(brew, "services", action, service) for action in ["start", "restart", "stop"]]
    listing = (brew, "services", "list", "--json")
    runner = fake_runner({**dict.fromkeys(commands, ""), listing: json.dumps([{"name": service, "status": "started", "user": "alice"}])})
    manager = ServiceManager(runtime, runner)
    assert manager.start(service).returncode == 0
    assert manager.restart(service).returncode == 0
    assert manager.stop(service).returncode == 0
    assert manager.status(service) == "active"
    assert runner.calls == [*commands, listing]


@pytest.mark.parametrize("port,name,pid", [("80", "httpd", "456"), ("443", "nginx", "999")])
def test_port_collision_stops_install(runtime, paths, runner, port, name, pid):
    runner.listeners = CommandResult(f"p{pid}\nc{name}\nn*:{port}\n")
    result = install(runtime, paths, runner)
    assert result.returncode != 0
    assert all(text in result.stderr for text in [port, name, pid])
    assert not runtime.nginx_launchd_plist.exists()
    assert not any("install" in call or "bootstrap" in call for call in runner.calls)


def test_install_is_idempotent(runtime, paths, runner):
    assert install(runtime, paths, runner).returncode == 0
    before = runtime.nginx_launchd_plist.stat().st_mtime_ns
    runner.listeners = CommandResult("p123\ncnginx\nn*:80\np124\ncnginx\nn*:443\n")
    assert install(runtime, paths, runner).returncode == 0
    assert runtime.nginx_launchd_plist.stat().st_mtime_ns == before
    assert sum("bootstrap" in call for call in runner.calls) == 1
    assert sum("install" in call for call in runner.calls) == 1
    assert not any("bootout" in call for call in runner.calls)


@pytest.mark.parametrize("content", [b"not a plist", b'<?xml version="1.0"?><plist><dict>',
                                    plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/usr/sbin/nginx"]})])
def test_install_refuses_non_owned_plist(runtime, paths, runner, content):
    runtime.nginx_launchd_plist.write_bytes(content)
    result = install(runtime, paths, runner)
    assert result.returncode != 0 and "owned" in result.stderr.lower()
    assert runtime.nginx_launchd_plist.read_bytes() == content
    assert runner.calls == []


def test_install_refuses_other_users_generated_plist(runtime, paths, runner):
    other = replace(runtime, runtime_user="bob")
    original = sites_platform.render_nginx_launchd_plist(other, *paths)
    runtime.nginx_launchd_plist.write_text(original)
    assert install(runtime, paths, runner).returncode != 0
    assert runtime.nginx_launchd_plist.read_text() == original and runner.calls == []


def test_install_refuses_symlink_even_to_generated_plist(runtime, paths, runner, tmp_path):
    target = tmp_path / "other.plist"
    original = sites_platform.render_nginx_launchd_plist(runtime, *paths)
    target.write_text(original)
    runtime.nginx_launchd_plist.symlink_to(target)
    assert install(runtime, paths, runner).returncode != 0
    assert runtime.nginx_launchd_plist.is_symlink() and target.read_text() == original
    assert runner.calls == []


def test_install_does_not_clobber_plist_created_during_publish(runtime, paths, runner):
    runner.before_install = lambda: runtime.nginx_launchd_plist.write_text("another installer")
    assert install(runtime, paths, runner).returncode != 0
    assert runtime.nginx_launchd_plist.read_text() == "another installer"
    assert not any("bootstrap" in call for call in runner.calls)


@pytest.mark.parametrize("listeners", ["p999\ncnginx\nn*:80\n", "p1000\ncnginx\nn*:443\n",
                                      "p124\ncnginx\nn*:80\np999\ncnginx\nn*:443\n"])
def test_loaded_service_does_not_authorize_unrelated_nginx(runtime, paths, runner, listeners):
    assert install(runtime, paths, runner).returncode == 0
    runner.calls.clear()
    runner.listeners = CommandResult(listeners)
    result = install(runtime, paths, runner)
    assert result.returncode != 0 and "nginx" in result.stderr and "ownership" in result.stderr.lower()
    assert not any("bootout" in call or "bootstrap" in call or "install" in call for call in runner.calls)


@pytest.mark.parametrize("response", [CommandResult("", "permission denied", 1),
                                     CommandResult("p123\ncnginx\nn*:80\n", "inspection incomplete", 1),
                                     CommandResult("garbage", "", 0), CommandResult("", "", 2)])
def test_failed_port_inspection_aborts_install(runtime, paths, runner, response):
    runner.listeners = response
    result = install(runtime, paths, runner)
    assert result.returncode != 0 and "ports" in result.stderr.lower()
    assert not runtime.nginx_launchd_plist.exists()


def test_unmatched_port_warning_preserves_owned_listener(runtime, paths, runner):
    assert install(runtime, paths, runner).returncode == 0
    runner.listeners = CommandResult("p124\ncnginx\nn*:80\n", "lsof: Internet address not located: TCP:443", 1)
    result = install(runtime, paths, runner)
    assert result.returncode == 0 and "TCP:443" in result.stderr


@pytest.mark.parametrize("tool", ["nginx", "install", "enable", "bootstrap"])
def test_install_propagates_command_failure(runtime, paths, runner, tool):
    failed = CommandResult("", "denied", 42)
    runner.failure = (lambda cmd: tool in cmd or (tool == "nginx" and str(runtime.brew_prefix / "bin/nginx") in cmd), failed)
    assert install(runtime, paths, runner) == failed
    assert not runner.loaded


def test_owned_plist_update_reloads_only_generated_job(runtime, paths, runner):
    assert install(runtime, paths, runner).returncode == 0
    replacement = paths[0].with_name("updated.conf")
    replacement.write_text(paths[0].read_text())
    runner.calls.clear()
    assert install(runtime, (replacement, paths[1]), runner).returncode == 0
    assert plistlib.loads(runtime.nginx_launchd_plist.read_bytes())["ProgramArguments"][2] == str(replacement)
    actions = [call[3:] for call in runner.calls if call[:3] == ("sudo", "-n", "launchctl")]
    assert actions == [("bootout", TARGET), ("enable", TARGET), ("bootstrap", "system", str(runtime.nginx_launchd_plist))]


def test_root_process_does_not_start_user_brew_services(runtime, fake_runner, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    runner = fake_runner({})
    result = ServiceManager(runtime, runner).start("mariadb")
    assert result.returncode != 0 and "user" in result.stderr.lower()
    assert runner.calls == []


def test_root_process_does_not_list_user_brew_services(runtime, fake_runner, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    runner = fake_runner({})
    assert ServiceManager(runtime, runner).status("mariadb") == "unknown"
    assert runner.calls == []


def test_nginx_actions_refuse_a_different_system_label(runtime, fake_runner):
    runner = fake_runner({})
    runtime = replace(runtime, nginx_launchd_label="com.other.nginx")
    assert ServiceManager(runtime, runner).stop("nginx").returncode != 0
    assert runner.calls == []


NGINX_ACTIONS = [("start", True), ("start", False), ("restart", True),
                 ("restart", False), ("stop", None), ("enable", None)]


def nginx_action(runtime, runner, action, persistent):
    kwargs = {} if persistent is None else {"persistent": persistent}
    return getattr(ServiceManager(runtime, runner), action)("nginx", **kwargs)


@pytest.mark.parametrize("action,persistent", NGINX_ACTIONS)
@pytest.mark.parametrize("kind", ["foreign", "other_user", "symlink", "missing"])
def test_direct_nginx_actions_refuse_unowned_plist(runtime, paths, runner, tmp_path, action, persistent, kind):
    path = runtime.nginx_launchd_plist
    original = sites_platform.render_nginx_launchd_plist(runtime, *paths)
    if kind == "foreign":
        path.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/tmp/foreign-nginx"]}))
    elif kind == "other_user":
        path.write_text(sites_platform.render_nginx_launchd_plist(replace(runtime, runtime_user="bob"), *paths))
    elif kind == "symlink":
        target = tmp_path / "symlink-target.plist"
        target.write_text(original)
        path.symlink_to(target)
    before = path.read_bytes() if path.exists() else None
    runner.loaded = action != "start"
    result = nginx_action(runtime, runner, action, persistent)
    assert result.returncode != 0 and "plist" in result.stderr.lower()
    assert runner.calls == []
    assert (path.read_bytes() if path.exists() else None) == before
    if kind == "symlink":
        assert path.is_symlink()


@pytest.mark.parametrize("action,persistent", NGINX_ACTIONS)
@pytest.mark.parametrize("job", [CommandResult("state = running\npath = /tmp/foreign.plist\npid = 123\n"),
                                 CommandResult("state = running\npid = 123\n"),
                                 CommandResult("", "Permission denied", 1)])
def test_direct_nginx_actions_refuse_unverified_loaded_job(runtime, paths, fake_runner, action, persistent, job):
    runtime.nginx_launchd_plist.write_text(sites_platform.render_nginx_launchd_plist(runtime, *paths))
    probe = ("launchctl", "print", TARGET)
    runner = fake_runner({probe: job})
    result = nginx_action(runtime, runner, action, persistent)
    assert result.returncode != 0
    assert runner.calls == [probe]


@pytest.mark.parametrize("action,persistent", NGINX_ACTIONS)
def test_direct_nginx_actions_allow_owned_plist_and_job(runtime, paths, runner, action, persistent):
    runtime.nginx_launchd_plist.write_text(sites_platform.render_nginx_launchd_plist(runtime, *paths))
    runner.loaded = action != "start"
    assert nginx_action(runtime, runner, action, persistent).returncode == 0
    expected = [("launchctl", "print", TARGET)]
    if persistent or action == "enable":
        expected.append(("sudo", "-n", "launchctl", "enable", TARGET))
    if action == "start":
        expected.append(("sudo", "-n", "launchctl", "bootstrap", "system", str(runtime.nginx_launchd_plist)))
    elif action == "restart":
        expected.append(("sudo", "-n", "launchctl", "kickstart", "-k", TARGET))
    elif action == "stop":
        expected.append(("sudo", "-n", "launchctl", "bootout", TARGET))
    assert runner.calls == expected


@pytest.mark.parametrize("content", [
    pytest.param(b'<?xml version="1.0"?><plist><date>not-a-date</date></plist>', id="invalid-date"),
    pytest.param(b'<?xml version="1.0" encoding="BOGUS"?><plist><dict/></plist>', id="unknown-encoding"),
])
@pytest.mark.parametrize("action,persistent", [("install", None), *NGINX_ACTIONS])
def test_malformed_plist_returns_controlled_refusal(runtime, paths, runner, content, action, persistent):
    runtime.nginx_launchd_plist.write_bytes(content)
    result = install(runtime, paths, runner) if action == "install" else nginx_action(runtime, runner, action, persistent)
    assert isinstance(result, CommandResult)
    assert result.returncode != 0 and "non-owned" in result.stderr
    assert runtime.nginx_launchd_plist.read_bytes() == content
    assert runner.calls == []


@pytest.mark.parametrize("output,state", [("unrecognized output", "unknown"),
                                         ("state = waiting\nlast exit code = -9", "failed")])
def test_launchd_status_does_not_hide_failure_or_bad_output(runtime, fake_runner, output, state):
    runner = fake_runner({("launchctl", "print", TARGET): output})
    assert ServiceManager(runtime, runner).status("nginx") == state
