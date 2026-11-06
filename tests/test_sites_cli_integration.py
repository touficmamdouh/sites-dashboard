import importlib.machinery
import importlib.util
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sites_config import load_config
from sites_platform import CommandResult, RuntimePaths


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "bin/sites"
    loader = importlib.machinery.SourceFileLoader("sites_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path):
    prefix = tmp_path / "brew"
    paths = RuntimePaths(
        platform="macos", architecture="arm64", brew_prefix=prefix,
        nginx_available=prefix / "etc/nginx/sites-available",
        nginx_enabled=prefix / "etc/nginx/sites-enabled",
        service_backend="launchd", php_service="php@8.4",
        php_socket=prefix / "var/run/custom.sock",
        php_binary=prefix / "opt/php@8.4/sbin/php-fpm",
        runtime_user="alice", runtime_group="staff", brew_binary=prefix / "bin/brew",
        nginx_launchd_label="com.sites-dashboard.nginx",
        nginx_launchd_plist=Path("/Library/LaunchDaemons/com.sites-dashboard.nginx.plist"),
    )
    return load_config({"HOME": str(tmp_path), "USER": "alice"}, paths)


@pytest.fixture
def commands(monkeypatch):
    calls = []

    def execute(command, **kwargs):
        calls.append(tuple(command))
        if "--json" in command:
            return CommandResult('[{"name":"php@8.4","status":"started"},'
                                 '{"name":"mailpit","status":"started"},'
                                 '{"name":"mariadb","status":"started"}]')
        if command[0] == "launchctl":
            return CommandResult("state = running")
        if command[:2] == ["systemctl", "is-active"]:
            return CommandResult("active")
        return CommandResult()

    return SimpleNamespace(calls=calls, run=execute)


def test_get_all_sites_uses_runtime_nginx_paths(cli, config):
    config.runtime.nginx_available.mkdir(parents=True)
    config.runtime.nginx_enabled.mkdir(parents=True)
    conf = config.runtime.nginx_available / "example.test.conf"
    conf.write_text(f"root {config.webroot}/example.test;\n"
                    f"fastcgi_pass unix:{config.runtime.php_socket};\n")
    (config.runtime.nginx_enabled / conf.name).symlink_to(conf)
    sites = cli.get_all_sites(config)
    assert len(sites) == 1
    assert sites[0]["name"] == "example.test"
    assert sites[0]["enabled"] is True
    assert sites[0]["root"] == str(config.webroot / "example.test")
    assert sites[0]["php"] == "8.4"


def test_get_service_status_uses_discovered_php_service(cli, config, commands):
    services = cli.get_service_status(config, runner=commands.run)
    assert services == [
        {"name": "nginx", "status": "active", "info": "Port 80/443"},
        {"name": "mailpit", "status": "active", "info": "UI http://127.0.0.1:8025"},
        {"name": "mariadb", "status": "active", "info": "Port 3306"},
        {"name": "php@8.4", "status": "active", "info": f"Socket {config.runtime.php_socket}"},
    ]
    assert all("systemctl" not in call for call in commands.calls)
    assert all("/run/php/php8.3-fpm.sock" not in arg for call in commands.calls for arg in call)


def test_start_uses_service_manager_for_all_runtime_services(cli, config, commands):
    cli.cli_start(config, runner=commands.run)
    brew = str(config.runtime.brew_binary)
    assert commands.calls[:5] == [
        (brew, "services", "start", "mariadb"),
        (brew, "services", "start", "mailpit"),
        (brew, "services", "start", "php@8.4"),
        ("sudo", "-n", "launchctl", "enable", "system/com.sites-dashboard.nginx"),
        ("sudo", "-n", "launchctl", "bootstrap", "system",
         "/Library/LaunchDaemons/com.sites-dashboard.nginx.plist"),
    ]
    assert all("systemctl" not in call for call in commands.calls)


def test_restart_named_service_does_not_restart_hardcoded_php(cli, config, commands):
    cli.cli_restart("mailpit", config, runner=commands.run)
    assert commands.calls == [(str(config.runtime.brew_binary), "services", "restart", "mailpit")]


@pytest.mark.parametrize("source", ["override", "sibling", "path"])
def test_create_resolves_installed_newsite_path(cli, config, monkeypatch, tmp_path, source):
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(cli, "__file__", str(installed / "sites"))
    sibling = installed / "newsite"
    elsewhere = tmp_path / "elsewhere/newsite"
    elsewhere.parent.mkdir()
    elsewhere.write_text("#!/bin/sh\n")
    elsewhere.chmod(0o755)
    if source != "path":
        sibling.write_text("#!/bin/sh\n")
        sibling.chmod(0o755)
    monkeypatch.delenv("SITES_NEWSITE", raising=False)
    if source == "override":
        monkeypatch.setenv("SITES_NEWSITE", str(elsewhere))
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(elsewhere))
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        return CommandResult(stdout="created")

    monkeypatch.setattr(cli.subprocess, "run", execute)
    monkeypatch.setattr(cli, "RUNTIME", config, raising=False)
    cli.cli_create("example.test", wp=True)
    expected = sibling if source == "sibling" else elsewhere
    assert calls == [[str(expected), "example.test", "--wp"]]


def test_help_does_not_discover_or_control_services(cli, monkeypatch, capsys):
    def forbidden():
        pytest.fail("Help must not discover services")

    monkeypatch.setattr(cli, "load_config", forbidden, raising=False)
    monkeypatch.setattr("sys.argv", ["sites", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for command in ("list", "check", "services", "enable", "disable", "start", "stop",
                    "restart", "create", "backup", "restore", "snapshot", "update-wordpress",
                    "update-all", "ssl", "export", "fix-permissions", "dashboard"):
        assert command in output


@pytest.mark.parametrize("action,names", [
    ("stop", ["nginx", "php@8.4", "mailpit", "mariadb"]),
    ("restart", ["mariadb", "mailpit", "php@8.4", "nginx"]),
])
def test_all_service_actions_use_runtime(cli, config, commands, action, names):
    if action == "stop":
        cli.cli_stop(config, runner=commands.run)
    else:
        cli.cli_restart(runtime=config, runner=commands.run)
    service_calls = [call for call in commands.calls if "services" in call and "--json" not in call]
    assert [call[-1] for call in service_calls] == [name for name in names if name != "nginx"]
    assert all("systemctl" not in call for call in commands.calls)
    assert any(("bootout" if action == "stop" else "kickstart") in call
               for call in commands.calls)


@pytest.mark.parametrize("action,target", [("start", None), ("restart", None), ("restart", "php8.6-fpm")])
def test_linux_service_actions_preserve_boot_configuration(cli, config, commands, action, target):
    linux = replace(config, runtime=replace(config.runtime, platform="linux",
                                           php_service="php8.6-fpm", service_backend="systemd"))
    if action == "start":
        cli.cli_start(linux, runner=commands.run)
    else:
        cli.cli_restart(target, linux, runner=commands.run)
    actions = [call for call in commands.calls if call[0] == "sudo"]
    services = [target] if target else ["mariadb", "mailpit", "php8.6-fpm", "nginx"]
    assert actions == [("sudo", "-n", "systemctl", action, service) for service in services]
    assert all("enable" not in call and "--now" not in call for call in commands.calls)


def test_service_failure_does_not_report_success(cli, config, capsys):
    cli.cli_restart("mailpit", config, runner=lambda cmd: CommandResult(stderr="denied", returncode=1))
    output = capsys.readouterr().out
    assert "denied" in output
    assert "mailpit restarted" not in output


def test_backup_restore_and_permissions_use_runtime(cli, config, monkeypatch, commands):
    monkeypatch.setattr(cli, "RUNTIME", config, raising=False)
    backup = cli.ensure_backup_dir("example.test")
    assert backup.parent == config.backup_dir / "example.test"
    cli.restore_site("example.test", backup)
    assert (config.webroot / "example.test").is_dir()
    content = config.webroot / "example.test/wp-content"
    content.mkdir()
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kwargs: commands.run(cmd))
    assert cli.fix_wp_permissions(content.parent)
    assert ("sudo", "-n", "chgrp", "-R", "staff", str(content)) in commands.calls
    assert all("usermod" not in call for call in commands.calls)


def test_runtime_is_loaded_once_at_command_startup(cli, config, monkeypatch):
    loads = []
    monkeypatch.setattr(cli, "load_config", lambda: loads.append(True) or config, raising=False)
    monkeypatch.setattr(cli, "cli_list", lambda: (cli.get_all_sites(), cli.get_all_sites()))
    monkeypatch.setattr("sys.argv", ["sites", "list"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert loads == [True]


def test_dashboard_refresh_renders_runtime_service_and_socket(cli, config, commands, monkeypatch):
    from textual.app import App
    from textual.widgets import Static

    apps = []
    monkeypatch.setattr(cli, "RUNTIME", config, raising=False)
    monkeypatch.setattr("sites_platform.run_command", commands.run)
    monkeypatch.setattr(App, "run", lambda self: apps.append(self))
    cli.run_dashboard()

    async def check():
        async with apps[0].run_test() as pilot:
            widget = apps[0].query_one("#svc-status", Static)
            text = str(widget.render())
            assert "php@8.4" in text
            assert str(config.runtime.php_socket) in text
            apps[0].refresh_all()
            assert str(config.runtime.php_socket) in str(widget.render())

    asyncio.run(check())


@pytest.mark.parametrize("width", [50, 80])
def test_dashboard_socket_is_visible_in_narrow_viewport(cli, config, commands, monkeypatch, width):
    from textual.app import App
    from textual.widgets import Static

    config = replace(config, runtime=replace(
        config.runtime,
        php_socket=Path("/opt/homebrew/var/run/" + "long-discovered-pool-directory/" * 4 + "php-fpm.sock"),
    ))
    apps = []
    monkeypatch.setattr(cli, "RUNTIME", config)
    monkeypatch.setattr("sites_platform.run_command", commands.run)
    monkeypatch.setattr(App, "run", lambda self: apps.append(self))
    cli.run_dashboard()

    async def check():
        async with apps[0].run_test(size=(width, 24)) as pilot:
            for _ in range(2):
                await pilot.pause()
                widget = apps[0].query_one("#svc-status", Static)
                region = widget.content_region.intersection(apps[0].screen.region)
                strips = apps[0].screen._compositor.render_strips()
                visible = "".join(
                    strips[y].crop(region.x, region.right).text
                    for y in range(region.y, region.bottom)
                )
                visible = "".join(visible.split())
                assert "php@8.4[active]" in visible
                assert str(config.runtime.php_socket) in visible
                apps[0].refresh_all()

    asyncio.run(check())
