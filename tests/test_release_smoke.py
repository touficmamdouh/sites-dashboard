import os
import subprocess
import sys
from pathlib import Path

from sites_config import load_config
from sites_platform import RuntimePaths


ROOT = Path(__file__).resolve().parents[1]


def _help(script: str) -> str:
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "bin")}
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / script), "--help"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output, _ = process.communicate(timeout=15)
    assert process.returncode == 0, output
    return output


def test_help_lists_existing_commands():
    output = _help("sites")
    for command in (
        "list", "check", "services", "enable", "disable", "start", "stop",
        "restart", "create", "backup", "restore", "snapshot", "update-wordpress",
        "update-all", "ssl", "export", "fix-permissions", "dashboard",
    ):
        assert command in output

    newsite_help = _help("newsite")
    assert "domain" in newsite_help
    assert "--wp" in newsite_help


def test_config_defaults_are_user_owned(tmp_path):
    brew = tmp_path / "homebrew"
    runtime = RuntimePaths(
        platform="macos",
        architecture="arm64",
        brew_prefix=brew,
        nginx_available=brew / "etc/nginx/sites-available",
        nginx_enabled=brew / "etc/nginx/sites-enabled",
        service_backend="launchd",
        php_service="php",
        php_socket=tmp_path / "run/php-fpm.sock",
        php_binary=brew / "opt/php/sbin/php-fpm",
        runtime_user="alice",
        runtime_group="staff",
        brew_binary=brew / "bin/brew",
        nginx_launchd_label="com.sites-dashboard.nginx",
        nginx_launchd_plist=tmp_path / "nginx.plist",
    )
    config = load_config({"HOME": str(tmp_path), "USER": "alice"}, runtime)
    assert config.webroot == tmp_path / "www"
    assert config.backup_dir == tmp_path / "backups"
    assert config.webroot.is_relative_to(tmp_path)
    assert config.backup_dir.is_relative_to(tmp_path)


def test_source_tree_has_no_unscoped_linux_assumptions():
    source = (ROOT / "bin/sites_platform.py").read_text()
    macos_adapter = source.split("class _MacOSAdapter:", 1)[1].split(
        "\ndef apply_runtime_overrides", 1
    )[0]
    for assumption in (
        "/etc/nginx",
        "/run/php",
        "/usr/sbin",
        "systemctl",
        "apt-get",
    ):
        assert assumption not in macos_adapter

    config_source = (ROOT / "bin/sites_config.py").read_text()
    assert "/etc/nginx" not in config_source
    assert "/run/php" not in config_source
