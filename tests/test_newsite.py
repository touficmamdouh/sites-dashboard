import importlib.machinery
import importlib.util
import io
import os
import shutil
import stat
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from sites_config import load_config
from sites_platform import CommandResult, RuntimePaths


@pytest.fixture
def newsite(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "bin/newsite"
    assert path.read_text().startswith("#!/usr/bin/env python3"), "newsite must use the shared Python runtime"
    loader = importlib.machinery.SourceFileLoader("newsite_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n::1 localhost\n")
    monkeypatch.setattr(module, "HOSTS_PATH", hosts)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    return module


@pytest.fixture(params=["linux", "macos-arm", "macos-intel"])
def config(tmp_path, request):
    mac = request.param.startswith("macos")
    prefix = tmp_path / ("opt/homebrew" if request.param == "macos-arm" else "usr/local")
    runtime = RuntimePaths(
        platform="macos" if mac else "linux", architecture="arm64" if request.param == "macos-arm" else "x86_64",
        brew_prefix=prefix if mac else None,
        nginx_available=prefix / "etc/nginx/sites-available",
        nginx_enabled=prefix / "etc/nginx/sites-enabled",
        service_backend="launchd" if mac else "systemd", php_service="php@8.4" if mac else "php8.6-fpm",
        php_socket=tmp_path / "php/custom.sock", php_binary=tmp_path / "php-fpm",
        runtime_user="alice" if mac else "www-data", runtime_group="staff" if mac else "www-data",
        brew_binary=prefix / "bin/brew" if mac else None,
        nginx_launchd_label="com.sites-dashboard.nginx",
        nginx_launchd_plist=tmp_path / "nginx.plist",
    )
    return load_config({"HOME": str(tmp_path), "USER": "alice", "SITES_DB_ROOT_USER": "admin",
                        "SITES_DB_ROOT_PASS": 'root-secret"\\\nvalue'}, runtime)


class HostRunner:
    """Emulate only the privileged/external boundary, within the test directory."""

    def __init__(self, root):
        self.root = root.resolve()
        self.calls = []
        self.fail = None
        self.listeners = ""
        self.db_files = []
        self.mysql_arguments = []

    def path(self, value):
        path = Path(value)
        assert path.is_absolute()
        assert path.is_relative_to(self.root), f"Host path escaped temporary directory: {path}"
        return path

    def __call__(self, command):
        self.calls.append(tuple(command))
        args = list(command)
        if args[:2] == ["sudo", "-n"]:
            args = args[2:]
        query_path = None
        if len(args) > 2 and args[1] == "-c":
            query_path = args[3]
            args = args[4:]
        if self.fail and (self.fail in args or self.fail == Path(args[0]).name):
            return CommandResult(stderr="injected failure", returncode=1)
        tool = Path(args[0]).name
        if tool == "mkdir":
            for value in args[2:]:
                self.path(value).mkdir(parents=True, exist_ok=True)
        elif tool == "install":
            source, target = map(self.path, args[-2:])
            assert source.stat().st_mode & 0o777 == 0o600
            shutil.copyfile(source, target)
            target.chmod(int(args[args.index("-m") + 1], 8))
        elif tool == "ln":
            source, target = map(self.path, args[-2:])
            if "-s" in args:
                target.symlink_to(source)
            else:
                os.link(source, target)
        elif tool == "mv":
            source, target = map(self.path, args[-2:])
            source.replace(target)
        elif tool == "rm":
            self.path(args[-1]).unlink(missing_ok=True)
        elif tool == "chown":
            self.path(args[-1])
        elif tool == "mysql":
            self.mysql_arguments = args
            defaults = self.path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--defaults-extra-file=")))
            query = self.path(query_path)
            assert stat.S_IMODE(defaults.stat().st_mode) == stat.S_IMODE(query.stat().st_mode) == 0o600
            self.db_files = [(defaults, defaults.read_text()), (query, query.read_text())]
        elif tool == "curl":
            archive = self.path(args[args.index("-o") + 1])
            with tarfile.open(archive, "w:gz") as bundle:
                for name in ("index.php", "wp-settings.php", "wp-includes/version.php"):
                    content = b"<?php // WordPress fixture\n"
                    entry = tarfile.TarInfo("wordpress/" + name)
                    entry.size = len(content)
                    bundle.addfile(entry, io.BytesIO(content))
        elif tool == "lsof":
            return CommandResult(self.listeners, "", 0 if self.listeners else 1)
        elif tool == "ss":
            return CommandResult('State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n' + self.listeners)
        elif tool == "systemctl":
            if args[1] == "is-active":
                return CommandResult("active")
            assert args[1:] == ["reload", "nginx"]
        elif tool == "launchctl":
            assert args[1:] == ["print", "system/com.sites-dashboard.nginx"]
            return CommandResult("state = running")
        elif tool == "nginx":
            assert args[1:] in (["-t"], ["-s", "reload"])
        else:
            pytest.fail(f"Unexpected external command: {args}")
        return CommandResult()


@pytest.fixture
def runner(tmp_path):
    return HostRunner(tmp_path)


def test_plain_site_writes_platform_nginx_config(newsite, config, runner, capsys):
    result = newsite.create_site("example.test", False, config, runner)
    assert result.success
    conf = config.runtime.nginx_available / "example.test.conf"
    text = conf.read_text()
    assert f"root {config.webroot}/example.test;" in text
    assert f"fastcgi_pass unix:{config.runtime.php_socket};" in text
    assert "listen 80;" in text and "listen [::]:80;" in text
    assert "server_name example.test;" in text and "$document_root$fastcgi_script_name" in text
    assert (config.runtime.nginx_enabled / conf.name).resolve() == conf
    assert (config.webroot / "example.test").is_dir()
    output = capsys.readouterr().out
    assert all(f"[{step}/7]" in output for step in range(1, 8))
    assert "Site created: http://example.test" in output
    assert "certbot --nginx -d example.test" in output
    assert not any("launchctl" in call and "bootstrap" in call for call in runner.calls)
    if config.runtime.platform == "macos":
        assert not any("systemctl" in call for call in runner.calls)


def test_wordpress_site_uses_runtime_owner(newsite, config, runner):
    assert newsite.create_site("blog.test", True, config, runner).success
    site = config.webroot / "blog.test"
    assert (site / "wp-includes/version.php").is_file()
    wp = (site / "wp-config.php").read_text()
    assert "define('DB_NAME', 'blog');" in wp
    assert wp.index("AUTH_KEY") < wp.index("require_once ABSPATH")
    assert "define('FS_METHOD', 'direct');" in wp
    assert stat.S_IMODE((site / "wp-config.php").stat().st_mode) == 0o640
    assert ("sudo", "-n", "chown", "-R", "alice:" + config.web_group, str(site)) in runner.calls
    assert config.db_root_pass not in wp


@pytest.mark.parametrize("mapping", ["127.0.0.1 example.test alias # comment\n", "::1 other example.test\n"])
def test_existing_hosts_mapping_is_preserved(newsite, config, runner, mapping):
    newsite.HOSTS_PATH.write_text(mapping)
    assert newsite.create_site("example.test", False, config, runner).success
    assert newsite.HOSTS_PATH.read_text() == mapping


def test_missing_hosts_mapping_is_added_once(newsite, config, runner):
    newsite.HOSTS_PATH.write_text("127.0.0.1 example.test.other # example.test\n")
    newsite.update_hosts("example.test", runner)
    once = newsite.HOSTS_PATH.read_text()
    newsite.update_hosts("example.test", runner)
    assert newsite.HOSTS_PATH.read_text() == once
    assert once.count("127.0.0.1\texample.test\n") == 1
    assert once.count("::1\texample.test\n") == 1


@pytest.mark.parametrize("mapping", ["10.0.0.5 alias example.test # existing\n",
                                       "127.0.0.1 example.test\n192.0.2.1 EXAMPLE.TEST\n"])
def test_conflicting_hosts_mapping_fails_before_changes(newsite, config, runner, mapping, capsys):
    newsite.HOSTS_PATH.write_text(mapping)
    result = newsite.create_site("example.test", False, config, runner)
    assert not result.success
    assert "example.test" in result.error.lower()
    assert mapping.splitlines()[-1] in result.error
    assert newsite.HOSTS_PATH.read_text() == mapping
    assert not config.webroot.exists()
    assert not runner.calls
    assert "Site created:" not in capsys.readouterr().out


def test_database_failure_does_not_report_success(newsite, config, runner, capsys):
    runner.fail = "mysql"
    result = newsite.create_site("example.test", False, config, runner)
    assert not result.success and "database" in result.error.lower()
    assert "Site created:" not in capsys.readouterr().out
    assert not any("reload" in call for call in runner.calls)
    assert not list(config.webroot.parent.glob("newsite-*"))


@pytest.mark.parametrize("port", [80, 443])
def test_nginx_port_collision_is_reported(newsite, config, runner, port, capsys):
    if config.runtime.platform == "linux":
        runner.listeners = f'LISTEN 0 128 0.0.0.0:{port} 0.0.0.0:* users:(("apache2",pid=456,fd=3))\n'
    else:
        runner.listeners = f"p456\ncapache2\nn*:{port}\n"
    result = newsite.create_site("example.test", False, config, runner)
    assert not result.success
    assert str(port) in result.error and "apache2" in result.error and "456" in result.error
    assert not config.webroot.exists()
    assert "Site created:" not in capsys.readouterr().out


def test_running_nginx_is_not_a_collision(newsite, config, runner):
    runner.listeners = ('LISTEN 0 128 [::]:80 [::]:* users:(("nginx",pid=123,fd=3))\n'
                        if config.runtime.platform == "linux" else "p123\ncnginx\nn*:80\n")
    assert newsite.create_site("example.test", False, config, runner).success


@pytest.mark.parametrize("domain", ["../outside", "-bad.test", "bad_.test", "a;touch.test", "a..test", "", "127.0.0.1", "x" * 64 + ".test"])
def test_invalid_domain_has_no_side_effects(newsite, config, runner, domain):
    assert not newsite.create_site(domain, False, config, runner).success
    assert not runner.calls and not config.webroot.exists()


@pytest.mark.parametrize("target", ["directory", "config", "enabled"])
def test_existing_site_targets_are_not_overwritten(newsite, config, runner, target):
    path = {"directory": config.webroot / "example.test", "config": config.runtime.nginx_available / "example.test.conf",
            "enabled": config.runtime.nginx_enabled / "example.test.conf"}[target]
    path.parent.mkdir(parents=True)
    path.symlink_to(path.parent / "missing")
    assert not newsite.create_site("example.test", True, config, runner).success
    assert path.is_symlink() and not runner.calls


def test_database_secrets_are_private_and_removed(newsite, config, runner, capsys):
    assert newsite.create_site("my-blog.test", False, config, runner).success
    assert all(config.db_root_pass not in arg for call in runner.calls for arg in call)
    assert not any("DROP USER" in content or "IF NOT EXISTS" in content for _, content in runner.db_files)
    defaults, sql = [content for _, content in runner.db_files]
    assert 'user="admin"' in defaults and 'password="root-secret\\"\\\\\\nvalue"' in defaults
    assert "CREATE DATABASE `my_blog`" in sql
    assert "CREATE USER 'my_blog'@'localhost'" in sql
    assert not any(path.exists() for path, _ in runner.db_files)
    assert "root-secret" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["install", "ln", "nginx", "reload", "chown", "curl"])
def test_command_failure_stops_success(newsite, config, runner, failure, capsys):
    runner.fail = failure
    assert not newsite.create_site("example.test", True, config, runner).success
    assert "Site created:" not in capsys.readouterr().out
    assert not list(config.webroot.parent.glob("newsite-*"))


def test_help_does_not_load_runtime(newsite, monkeypatch, capsys):
    monkeypatch.setattr(newsite, "load_config", lambda: pytest.fail("help must not discover services"))
    assert newsite.main(["--help"]) == 0
    assert "newsite" in capsys.readouterr().out


def test_cli_failure_returns_nonzero(newsite, config, runner, monkeypatch):
    runner.fail = "mysql"
    monkeypatch.setattr(newsite, "load_config", lambda: config)
    monkeypatch.setattr(newsite, "run_command", runner)
    assert newsite.main(["example.test"]) == 1


def test_mysql_uses_batch_stdin_and_aborts_errors(newsite, config, runner):
    assert newsite.create_site("example.test", False, config, runner).success
    assert "--batch" in runner.mysql_arguments and "--skip-force" in runner.mysql_arguments
    assert not any("source " in arg for arg in runner.mysql_arguments)


def test_macos_uses_discovered_brew_binaries(newsite, config, runner):
    if config.runtime.platform == "linux":
        return
    assert newsite.create_site("example.test", False, config, runner).success
    for name in ("mysql", "nginx"):
        assert any(str(config.runtime.brew_prefix / "bin" / name) in call for call in runner.calls)


def test_wordpress_permissions_survive_restrictive_umask(newsite, config, runner):
    old = os.umask(0o077)
    try:
        assert newsite.create_site("example.test", True, config, runner).success
    finally:
        os.umask(old)
    site = config.webroot / "example.test"
    assert stat.S_IMODE(site.stat().st_mode) == 0o755
    assert stat.S_IMODE((site / "wp-includes").stat().st_mode) == 0o755


def test_archive_traversal_is_rejected(newsite, config, runner):
    def malicious_download(command):
        if command[0] == "curl":
            with tarfile.open(command[command.index("-o") + 1], "w:gz") as bundle:
                entry = tarfile.TarInfo("wordpress/../../outside")
                entry.size = 1
                bundle.addfile(entry, io.BytesIO(b"x"))
            return CommandResult()
        return runner(command)
    result = newsite.create_site("example.test", True, config, malicious_download)
    assert not result.success and "Unsafe" in result.error
    assert not (config.webroot / "outside").exists()


def test_nginx_paths_with_spaces_are_quoted(newsite, config, runner):
    config = replace(config, webroot=config.webroot / "my sites", runtime=replace(config.runtime, php_socket=config.webroot / "php sockets/fpm.sock"))
    assert newsite.create_site("example.test", False, config, runner).success
    text = (config.runtime.nginx_available / "example.test.conf").read_text()
    assert f'root "{config.webroot}/example.test";' in text
    assert f'fastcgi_pass "unix:{config.runtime.php_socket}";' in text
