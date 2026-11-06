import json
from pathlib import Path

import pytest

from sites_platform import CommandResult, ServiceManager, detect_runtime, run_command


def mac_runner(fake_runner, prefix="/opt/homebrew", service="php@8.3", status="started", listen=None):
    return fake_runner({
        (f"{prefix}/bin/brew", "--prefix"): prefix + "\n",
        (f"{prefix}/bin/brew", "services", "list", "--json"): json.dumps([
            {"name": "php", "status": "none", "user": None, "file": None},
            {"name": service, "status": status, "user": "alice", "file": "php.plist"},
        ]),
        (f"{prefix}/opt/{service}/sbin/php-fpm", "-tt"): CommandResult(
            "", f"[notice] listen = {listen or prefix + '/var/run/php-fpm.sock'}\n", 0
        ),
    })


def assert_mac(runtime, prefix, machine):
    assert runtime.platform == "macos"
    assert runtime.architecture == machine
    assert runtime.brew_prefix == Path(prefix)
    assert runtime.nginx_available == Path(prefix + "/etc/nginx/sites-available")
    assert runtime.nginx_enabled == Path(prefix + "/etc/nginx/sites-enabled")
    assert runtime.service_backend == "launchd"
    assert runtime.php_service == "php@8.3"
    assert runtime.php_socket == Path(prefix + "/var/run/php-fpm.sock")
    assert runtime.runtime_user == "alice"
    assert runtime.runtime_group == "staff"
    assert runtime.nginx_launchd_label == "com.sites-dashboard.nginx"


def test_detect_macos_arm_uses_opt_homebrew(host, fake_runner):
    assert_mac(detect_runtime({}, mac_runner(fake_runner)), "/opt/homebrew", "arm64")


def test_detect_macos_intel_uses_usr_local_homebrew(host, fake_runner):
    host(machine="x86_64", brew="/usr/local/bin/brew")
    assert_mac(detect_runtime({}, mac_runner(fake_runner, "/usr/local")), "/usr/local", "x86_64")


def test_detect_linux_keeps_existing_nginx_paths(host, fake_runner):
    host("Linux", "x86_64", None)
    runner = fake_runner({
        ("systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager"):
            "php8.2-fpm.service loaded inactive dead PHP\nphp8.3-fpm.service loaded active running PHP\n",
        ("/usr/sbin/php-fpm8.3", "-tt"): CommandResult("", "unavailable", 127),
    })
    runtime = detect_runtime({}, runner)
    assert runtime.platform == "linux"
    assert runtime.brew_prefix is None
    assert runtime.nginx_available == Path("/etc/nginx/sites-available")
    assert runtime.nginx_enabled == Path("/etc/nginx/sites-enabled")
    assert runtime.service_backend == "systemd"
    assert runtime.php_service == "php8.3-fpm"
    assert runtime.php_socket == Path("/run/php/php8.3-fpm.sock")
    assert runtime.runtime_user == runtime.runtime_group == "www-data"


@pytest.mark.parametrize("system,service,socket", [
    ("Darwin", "php@8.4", "/tmp/mac-php.sock"),
    ("Linux", "php8.4-fpm", "/tmp/linux-php.sock"),
])
def test_php_service_override_wins(host, fake_runner, system, service, socket):
    host(system)
    runner = fake_runner({})
    runtime = detect_runtime({
        "SITES_BREW_PREFIX": "/opt/homebrew", "SITES_PHP_SERVICE": service,
        "SITES_PHP_SVC": "old-service", "SITES_PHP_SOCK": socket,
    }, runner)
    assert runtime.php_service == service
    assert runtime.php_socket == Path(socket)
    assert runner.calls == []


def test_missing_homebrew_has_actionable_error(host, fake_runner):
    host(brew=None)
    with pytest.raises(RuntimeError, match="Homebrew.*brew.sh.*SITES_BREW_PREFIX"):
        detect_runtime({}, fake_runner({}))


def test_runtime_user_is_not_www_data_on_macos(host, fake_runner):
    runtime = detect_runtime({}, mac_runner(fake_runner))
    assert runtime.runtime_user == "alice"
    assert runtime.runtime_user != "www-data"


def test_custom_prefix_is_independent_of_architecture(host, fake_runner):
    host(machine="arm64", brew="/custom/brew/bin/brew")
    assert_mac(detect_runtime({}, mac_runner(fake_runner, "/custom/brew")), "/custom/brew", "arm64")


def test_environment_prefix_prevents_brew_prefix_probe(host, fake_runner):
    runner = mac_runner(fake_runner, "/custom/brew")
    assert detect_runtime({"SITES_BREW_PREFIX": "/custom/brew"}, runner).brew_prefix == Path("/custom/brew")
    assert ("/custom/brew/bin/brew", "--prefix") not in runner.calls


def test_failed_prefix_probe_never_guesses_a_prefix(host, fake_runner):
    runner = fake_runner({("/opt/homebrew/bin/brew", "--prefix"): CommandResult("", "broken", 1)})
    with pytest.raises(RuntimeError, match="SITES_BREW_PREFIX"):
        detect_runtime({}, runner)


def test_macos_uses_configured_socket(host, fake_runner):
    runtime = detect_runtime({}, mac_runner(fake_runner, listen="/tmp/custom-php.sock"))
    assert runtime.php_socket == Path("/tmp/custom-php.sock")


def test_macos_tcp_listener_has_actionable_error(host, fake_runner):
    with pytest.raises(RuntimeError, match="SITES_PHP_SOCK"):
        detect_runtime({}, mac_runner(fake_runner, listen="127.0.0.1:9000"))


def test_missing_php_has_actionable_error(host, fake_runner):
    runner = fake_runner({
        ("/opt/homebrew/bin/brew", "--prefix"): "/opt/homebrew",
        ("/opt/homebrew/bin/brew", "services", "list", "--json"): "[]",
    })
    with pytest.raises(RuntimeError, match="PHP.*SITES_PHP_SERVICE"):
        detect_runtime({}, runner)


def test_linux_installed_php_fallback(host, fake_runner):
    host("Linux")
    runner = fake_runner({
        ("systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager"): "",
        ("systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"):
            "php8.4-fpm.service enabled enabled\n",
        ("/usr/sbin/php-fpm8.4", "-tt"): CommandResult("", "unavailable", 127),
    })
    assert detect_runtime({}, runner).php_socket == Path("/run/php/php8.4-fpm.sock")


def test_unsupported_platform_fails(host, fake_runner):
    host("Windows")
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        detect_runtime({}, fake_runner({}))


@pytest.fixture
def mac_runtime(host, fake_runner):
    return detect_runtime({}, mac_runner(fake_runner))


@pytest.fixture
def linux_runtime(host, fake_runner):
    host("Linux")
    return detect_runtime({"SITES_PHP_SERVICE": "php8.3-fpm"}, fake_runner({}))


@pytest.mark.parametrize("method,persistent,commands", [
    ("start", True, [("enable", "--now", "php8.3-fpm")]),
    ("start", False, [("start", "php8.3-fpm")]),
    ("restart", True, [("enable", "php8.3-fpm"), ("restart", "php8.3-fpm")]),
    ("restart", False, [("restart", "php8.3-fpm")]),
    ("stop", None, [("stop", "php8.3-fpm")]),
    ("enable", None, [("enable", "php8.3-fpm")]),
])
def test_linux_service_actions(linux_runtime, fake_runner, method, persistent, commands):
    expected = [("sudo", "-n", "systemctl", *cmd) for cmd in commands]
    runner = fake_runner(dict.fromkeys(expected, ""))
    manager = ServiceManager(linux_runtime, runner)
    result = getattr(manager, method)("php8.3-fpm", **({"persistent": persistent} if persistent is not None else {}))
    assert result.returncode == 0
    assert runner.calls == expected


@pytest.mark.parametrize("service", ["php@8.3", "mariadb", "mailpit"])
@pytest.mark.parametrize("method,persistent,actions", [
    ("start", True, ["start"]), ("start", False, ["run"]),
    ("restart", True, ["restart"]), ("restart", False, ["stop", "run"]),
    ("stop", None, ["stop"]), ("enable", None, ["start"]),
])
def test_brew_service_actions_are_user_scoped(mac_runtime, fake_runner, service, method, persistent, actions):
    expected = [("/opt/homebrew/bin/brew", "services", action, service) for action in actions]
    runner = fake_runner(dict.fromkeys(expected, ""))
    manager = ServiceManager(mac_runtime, runner)
    result = getattr(manager, method)(service, **({"persistent": persistent} if persistent is not None else {}))
    assert result.returncode == 0
    assert runner.calls == expected


@pytest.mark.parametrize("method,actions", [
    ("start", [("enable", "system/com.sites-dashboard.nginx"),
               ("bootstrap", "system", "/Library/LaunchDaemons/com.sites-dashboard.nginx.plist")]),
    ("restart", [("enable", "system/com.sites-dashboard.nginx"),
                 ("kickstart", "-k", "system/com.sites-dashboard.nginx")]),
    ("stop", [("bootout", "system/com.sites-dashboard.nginx")]),
    ("enable", [("enable", "system/com.sites-dashboard.nginx")]),
])
def test_nginx_uses_generated_system_launchd_label(mac_runtime, fake_runner, method, actions):
    expected = [("sudo", "-n", "launchctl", *action) for action in actions]
    runner = fake_runner(dict.fromkeys(expected, ""))
    assert getattr(ServiceManager(mac_runtime, runner), method)("nginx").returncode == 0
    assert runner.calls == expected


@pytest.mark.parametrize("raw,expected", [("started", "active"), ("stopped", "inactive"), ("error", "failed"), ("other", "unknown")])
def test_brew_status_normalized(mac_runtime, fake_runner, raw, expected):
    runner = fake_runner({("/opt/homebrew/bin/brew", "services", "list", "--json"):
                          json.dumps([{"name": "php@8.3", "status": raw}])})
    assert ServiceManager(mac_runtime, runner).status("php@8.3") == expected


@pytest.mark.parametrize("output,code,expected", [("active", 0, "active"), ("failed", 3, "failed"), ("inactive", 3, "inactive"), ("", 1, "unknown")])
def test_systemd_status_normalized(linux_runtime, fake_runner, output, code, expected):
    runner = fake_runner({("systemctl", "is-active", "nginx"): CommandResult(output, "", code)})
    assert ServiceManager(linux_runtime, runner).status("nginx") == expected


@pytest.mark.parametrize("output,stderr,code,expected", [
    ("state = running", "", 0, "active"), ("state = waiting\nlast exit code = 0", "", 0, "inactive"),
    ("state = waiting\nlast exit code = 78", "", 0, "failed"),
    ("", "Could not find service", 113, "inactive"), ("", "Permission denied", 1, "unknown"),
])
def test_launchd_status_normalized(mac_runtime, fake_runner, output, stderr, code, expected):
    runner = fake_runner({("launchctl", "print", "system/com.sites-dashboard.nginx"): CommandResult(output, stderr, code)})
    assert ServiceManager(mac_runtime, runner).status("nginx") == expected


def test_command_failure_stops_sequence(linux_runtime, fake_runner):
    failed = CommandResult("", "permission denied", 1)
    runner = fake_runner({("sudo", "-n", "systemctl", "enable", "nginx"): failed})
    assert ServiceManager(linux_runtime, runner).restart("nginx") == failed
    assert len(runner.calls) == 1


def test_linux_discovers_configured_socket_for_active_php(host, fake_runner):
    host("Linux")
    runner = fake_runner({
        ("systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager"):
            "php8.5-fpm.service loaded active running PHP\n",
        ("/usr/sbin/php-fpm8.5", "-tt"):
            CommandResult("", "[notice] listen = /tmp/phpwebstudy-php-cgi-85.sock\n", 0),
    })
    assert detect_runtime({}, runner).php_socket == Path("/tmp/phpwebstudy-php-cgi-85.sock")


def test_php_socket_path_with_spaces_is_preserved(host, fake_runner):
    runtime = detect_runtime({}, mac_runner(fake_runner, listen="/tmp/my php.sock"))
    assert runtime.php_socket == Path("/tmp/my php.sock")


@pytest.mark.parametrize("listing", ["not json", "{}", "[null]"])
def test_invalid_brew_listing_returns_unknown_status(mac_runtime, fake_runner, listing):
    runner = fake_runner({("/opt/homebrew/bin/brew", "services", "list", "--json"): listing})
    assert ServiceManager(mac_runtime, runner).status("php@8.3") == "unknown"


def test_brew_command_failure_returns_unknown_status(mac_runtime, fake_runner):
    runner = fake_runner({("/opt/homebrew/bin/brew", "services", "list", "--json"):
                          CommandResult("", "unavailable", 1)})
    assert ServiceManager(mac_runtime, runner).status("php@8.3") == "unknown"


def test_macos_service_change_requires_matching_socket(mac_runtime):
    from sites_config import load_config

    with pytest.raises(RuntimeError, match="SITES_PHP_SOCK"):
        load_config({"SITES_PHP_SERVICE": "php@8.4"}, mac_runtime)


def test_transient_brew_restart_preserves_stop_failure(mac_runtime, fake_runner):
    failed = CommandResult("", "cannot stop", 1)
    runner = fake_runner({("/opt/homebrew/bin/brew", "services", "stop", "mariadb"): failed})
    assert ServiceManager(mac_runtime, runner).restart("mariadb", persistent=False) == failed
    assert len(runner.calls) == 1


@pytest.mark.parametrize("method,action", [("start", ("bootstrap", "system", "/Library/LaunchDaemons/com.sites-dashboard.nginx.plist")),
                                          ("restart", ("kickstart", "-k", "system/com.sites-dashboard.nginx"))])
def test_transient_nginx_action_does_not_enable(mac_runtime, fake_runner, method, action):
    command = ("sudo", "-n", "launchctl", *action)
    runner = fake_runner({command: ""})
    assert getattr(ServiceManager(mac_runtime, runner), method)("nginx", persistent=False).returncode == 0
    assert runner.calls == [command]


def test_default_runner_preserves_output_and_exit_code(monkeypatch):
    import subprocess

    def execute(command, **kwargs):
        assert command == ["example", "literal;argument"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 15}
        return subprocess.CompletedProcess(command, 7, " output\n", " error\n")

    monkeypatch.setattr("subprocess.run", execute)
    assert run_command(["example", "literal;argument"]) == CommandResult("output", "error", 7)


@pytest.mark.parametrize("error,message", [(FileNotFoundError(), "Command not found"), (TimeoutError(), "")])
def test_runner_missing_command_is_structured(monkeypatch, error, message):
    if isinstance(error, TimeoutError):
        import subprocess
        error = subprocess.TimeoutExpired(["missing"], 15)
        message = "Timed out"

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("subprocess.run", fail)
    result = run_command(["missing"])
    assert result.returncode != 0
    assert message in result.stderr
