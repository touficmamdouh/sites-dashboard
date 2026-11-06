from pathlib import Path

import pytest

from sites_config import load_config
from sites_platform import detect_runtime


@pytest.fixture
def runtime(host, fake_runner):
    host("Linux")
    return detect_runtime({"SITES_PHP_SERVICE": "php8.3-fpm"}, fake_runner({}))


def test_defaults_use_current_home(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config({}, runtime)
    assert config.webroot == tmp_path / "www"
    assert config.backup_dir == tmp_path / "backups"
    assert config.runtime == runtime
    assert config.db_root_user == config.db_root_pass == "root"
    assert config.web_user == "alice"
    assert not config.webroot.exists()
    assert not config.backup_dir.exists()
    assert "db_root_pass" not in repr(config)


def test_existing_environment_overrides_are_preserved(runtime):
    config = load_config({
        "HOME": "/Users/bob", "SITES_WEBROOT": "~/sites", "SITES_BACKUP_DIR": "/data/backups",
        "NGINX_AVAILABLE": "/custom/available", "NGINX_ENABLED": "/custom/enabled",
        "SITES_PHP_SVC": "php8.4-fpm", "SITES_PHP_SOCK": "/custom/php.sock",
        "SITES_WEB_USER": "bob", "SITES_WEB_GROUP": "editors",
        "SITES_DB_ROOT_USER": "admin", "SITES_DB_ROOT_PASS": "secret",
    }, runtime)
    assert config.webroot == Path("/Users/bob/sites")
    assert config.backup_dir == Path("/data/backups")
    assert config.runtime.nginx_available == Path("/custom/available")
    assert config.runtime.nginx_enabled == Path("/custom/enabled")
    assert config.runtime.php_service == "php8.4-fpm"
    assert config.runtime.php_socket == Path("/custom/php.sock")
    assert config.web_user == "bob"
    assert config.web_group == "editors"
    assert config.db_root_user == "admin"
    assert config.db_root_pass == "secret"
    assert "secret" not in repr(config)
    assert runtime.php_service == "php8.3-fpm"


def test_config_uses_process_environment_only_when_env_is_none(runtime, monkeypatch):
    monkeypatch.setenv("SITES_WEBROOT", "/from/process")
    assert load_config(runtime=runtime).webroot == Path("/from/process")
    assert load_config({"HOME": "/isolated"}, runtime).webroot == Path("/isolated/www")


def test_new_php_override_beats_legacy_override(runtime):
    config = load_config({"SITES_PHP_SERVICE": "php8.5-fpm", "SITES_PHP_SVC": "php8.4-fpm"}, runtime)
    assert config.runtime.php_service == "php8.5-fpm"
    assert config.runtime.php_socket == Path("/run/php/php8.5-fpm.sock")


def test_nginx_prefix_override_is_shared_by_detection_and_config(host, fake_runner):
    host("Linux")
    env = {"SITES_PHP_SERVICE": "php8.3-fpm", "SITES_NGINX_PREFIX": "/managed/nginx"}
    runtime = detect_runtime(env, fake_runner({}))
    assert runtime.nginx_available == Path("/managed/nginx/sites-available")
    assert load_config(env, runtime).runtime.nginx_enabled == Path("/managed/nginx/sites-enabled")


def test_config_without_runtime_delegates_detection(monkeypatch, runtime):
    seen = []

    def detect(env=None):
        seen.append(env)
        return runtime

    monkeypatch.setattr("sites_config.detect_runtime", detect)
    env = {"HOME": "/Users/alice"}
    assert load_config(env).runtime == runtime
    assert seen == [env]


def test_empty_database_password_is_preserved(runtime):
    assert load_config({"SITES_DB_ROOT_PASS": ""}, runtime).db_root_pass == ""
