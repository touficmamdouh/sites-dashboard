"""Platform discovery and service commands, without import-time side effects."""

from __future__ import annotations

import getpass
import grp
import json
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


CommandRunner = Callable[[Sequence[str]], CommandResult]


def run_command(command: Sequence[str]) -> CommandResult:
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, timeout=15)
        return CommandResult(result.stdout.strip(), result.stderr.strip(), result.returncode)
    except FileNotFoundError:
        return CommandResult(stderr=f"Command not found: {command[0]}", returncode=127)
    except subprocess.TimeoutExpired:
        return CommandResult(stderr=f"Timed out: {command[0]}", returncode=124)


@dataclass(frozen=True)
class RuntimePaths:
    platform: str
    architecture: str
    brew_prefix: Path | None
    nginx_available: Path
    nginx_enabled: Path
    service_backend: str
    php_service: str
    php_socket: Path
    php_binary: Path
    runtime_user: str
    runtime_group: str
    brew_binary: Path | None = None
    nginx_launchd_label: str | None = None
    nginx_launchd_plist: Path | None = None


def expand_path(value: str, env: Mapping[str, str]) -> Path:
    home = Path(env.get("HOME") or Path.home())
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _php_override(env: Mapping[str, str]) -> str | None:
    return env.get("SITES_PHP_SERVICE") or env.get("SITES_PHP_SVC") or None


def _brew_services(brew: Path, runner: CommandRunner) -> list[dict]:
    result = runner([str(brew), "services", "list", "--json"])
    if result.returncode:
        raise RuntimeError(f"Cannot list Homebrew services: {result.stderr}")
    try:
        services = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("Homebrew returned an invalid service listing") from error
    if not isinstance(services, list) or any(not isinstance(item, dict) for item in services):
        raise RuntimeError("Homebrew returned an invalid service listing")
    return services


class _LinuxAdapter:
    @staticmethod
    def php_paths(service: str) -> tuple[Path, Path]:
        version = re.fullmatch(r"php([0-9.]+)-fpm", service)
        binary = "php-fpm" + (version[1] if version else "")
        return Path(f"/run/php/{service}.sock"), Path("/usr/sbin") / binary

    @classmethod
    def detect(cls, env: Mapping[str, str], runner: CommandRunner) -> RuntimePaths:
        service = _php_override(env)
        if not service:
            units = runner(["systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager"])
            candidates = []
            for line in units.stdout.splitlines() if not units.returncode else []:
                fields = line.lstrip("● ").split()
                if fields and re.fullmatch(r"php[0-9.]+-fpm\.service", fields[0]):
                    candidates.append(("active" in fields[1:4], fields[0].removesuffix(".service")))
            if candidates:
                service = sorted(candidates, key=lambda item: not item[0])[0][1]
            else:
                installed = runner(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
                match = re.search(r"(?m)^(php[0-9.]+-fpm)\.service\s", installed.stdout) if not installed.returncode else None
                service = match[1] if match else None
        if not service:
            raise RuntimeError("No PHP-FPM service found. Install PHP-FPM or set SITES_PHP_SERVICE and SITES_PHP_SOCK.")
        socket, binary = cls.php_paths(service)
        runtime = RuntimePaths(
            "linux", platform.machine(), None,
            Path("/etc/nginx/sites-available"), Path("/etc/nginx/sites-enabled"),
            "systemd", service, socket, binary, "www-data", "www-data",
        )
        return apply_runtime_overrides(runtime, env)

    @staticmethod
    def status(runtime: RuntimePaths, service: str, runner: CommandRunner) -> str:
        result = runner(["systemctl", "is-active", service])
        state = result.stdout.strip()
        return state if state in {"active", "inactive", "failed"} else "unknown"

    @staticmethod
    def action(runtime: RuntimePaths, service: str, action: str, persistent: bool, runner: CommandRunner) -> CommandResult:
        if action == "start" and persistent:
            return runner(["sudo", "-n", "systemctl", "enable", "--now", service])
        if action == "restart" and persistent:
            result = runner(["sudo", "-n", "systemctl", "enable", service])
            if result.returncode:
                return result
        return runner(["sudo", "-n", "systemctl", action, service])


class _MacOSAdapter:
    @staticmethod
    def php_binary(prefix: Path, service: str) -> Path:
        return prefix / "opt" / service / "sbin/php-fpm"

    @classmethod
    def detect(cls, env: Mapping[str, str], runner: CommandRunner) -> RuntimePaths:
        prefix_override = env.get("SITES_BREW_PREFIX")
        brew = Path(prefix_override) / "bin/brew" if prefix_override else shutil.which("brew")
        diagnostic = (
            "Homebrew is required: install it from https://brew.sh, add brew to PATH, "
            "or set SITES_BREW_PREFIX. Check /opt/homebrew and /usr/local if already installed."
        )
        if not brew:
            raise RuntimeError(diagnostic)
        brew = Path(brew)
        if prefix_override:
            prefix = expand_path(prefix_override, env)
            brew = prefix / "bin/brew"
        else:
            result = runner([str(brew), "--prefix"])
            if result.returncode or not result.stdout.strip():
                raise RuntimeError(f"{diagnostic} brew --prefix failed: {result.stderr}")
            prefix = Path(result.stdout.strip())
        if not prefix.is_absolute():
            raise RuntimeError(f"{diagnostic} Homebrew prefix must be an absolute path.")
        service = _php_override(env)
        if not service:
            services = _brew_services(brew, runner)
            candidates = [item for item in services if re.fullmatch(r"php(?:@[0-9.]+)?", item.get("name", ""))]
            candidates.sort(key=lambda item: item.get("status") != "started")
            service = candidates[0]["name"] if candidates else None
        if not service:
            raise RuntimeError("No PHP-FPM service found. Run brew install php or set SITES_PHP_SERVICE and SITES_PHP_SOCK.")
        binary = cls.php_binary(prefix, service)
        socket = env.get("SITES_PHP_SOCK")
        if not socket:
            result = runner([str(binary), "-tt"])
            # php-fpm writes its configuration dump to stderr, including notices.
            listeners = re.findall(r"\blisten\s*=\s*(\S+)", result.stdout + "\n" + result.stderr)
            sockets = set(value for value in listeners if value.startswith("/"))
            if result.returncode or len(sockets) != 1 or any(not value.startswith("/") for value in listeners):
                raise RuntimeError(
                    f"Cannot resolve a single PHP-FPM Unix socket for {service}. "
                    "Configure the PHP-FPM pool listen path or set SITES_PHP_SOCK explicitly."
                )
            socket = sockets.pop()
        user = env.get("SUDO_USER") or env.get("USER") or getpass.getuser()
        runtime = RuntimePaths(
            "macos", platform.machine(), prefix,
            prefix / "etc/nginx/sites-available", prefix / "etc/nginx/sites-enabled",
            "launchd", service, expand_path(socket, env), binary,
            user, grp.getgrgid(os.getgid()).gr_name, brew,
            "com.sites-dashboard.nginx", Path("/Library/LaunchDaemons/com.sites-dashboard.nginx.plist"),
        )
        return apply_runtime_overrides(runtime, env)

    @staticmethod
    def status(runtime: RuntimePaths, service: str, runner: CommandRunner) -> str:
        if service == "nginx":
            result = runner(["launchctl", "print", f"system/{runtime.nginx_launchd_label}"])
            if result.returncode:
                return "inactive" if "Could not find service" in result.stderr else "unknown"
            if re.search(r"\bstate\s*=\s*running\b", result.stdout):
                return "active"
            exit_code = re.search(r"last exit code\s*=\s*(\d+)", result.stdout)
            return "failed" if exit_code and int(exit_code[1]) else "inactive"
        try:
            services = _brew_services(runtime.brew_binary, runner)
        except RuntimeError:
            return "unknown"
        state = next((item.get("status") for item in services if item.get("name") == service), None)
        return {"started": "active", "stopped": "inactive", "none": "inactive", "error": "failed"}.get(state, "unknown")

    @staticmethod
    def action(runtime: RuntimePaths, service: str, action: str, persistent: bool, runner: CommandRunner) -> CommandResult:
        if service == "nginx":
            target = f"system/{runtime.nginx_launchd_label}"
            if action in {"start", "restart"} and persistent:
                result = runner(["sudo", "-n", "launchctl", "enable", target])
                if result.returncode:
                    return result
            commands = {
                "start": ["bootstrap", "system", str(runtime.nginx_launchd_plist)],
                "restart": ["kickstart", "-k", target],
                "stop": ["bootout", target],
                "enable": ["enable", target],
            }
            return runner(["sudo", "-n", "launchctl", *commands[action]])
        base = [str(runtime.brew_binary), "services"]
        if action == "restart" and not persistent:
            result = runner([*base, "stop", service])
            if result.returncode:
                return result
        brew_action = "start" if action == "enable" else action
        if action in {"start", "restart"} and not persistent:
            brew_action = "run"
        return runner([*base, brew_action, service])


def apply_runtime_overrides(runtime: RuntimePaths, env: Mapping[str, str]) -> RuntimePaths:
    changes = {}
    if env.get("SITES_NGINX_PREFIX"):
        prefix = expand_path(env["SITES_NGINX_PREFIX"], env)
        changes.update(nginx_available=prefix / "sites-available", nginx_enabled=prefix / "sites-enabled")
    for variable, field in (("NGINX_AVAILABLE", "nginx_available"), ("NGINX_ENABLED", "nginx_enabled")):
        if env.get(variable):
            changes[field] = expand_path(env[variable], env)
    service = _php_override(env)
    if service and service != runtime.php_service:
        changes["php_service"] = service
        if runtime.platform == "linux":
            changes["php_socket"], changes["php_binary"] = _LinuxAdapter.php_paths(service)
        else:
            changes["php_binary"] = _MacOSAdapter.php_binary(runtime.brew_prefix, service)
            if not env.get("SITES_PHP_SOCK"):
                raise RuntimeError("Changing PHP service requires SITES_PHP_SOCK when reusing a detected runtime.")
    if env.get("SITES_PHP_SOCK"):
        changes["php_socket"] = expand_path(env["SITES_PHP_SOCK"], env)
    return replace(runtime, **changes) if changes else runtime


def detect_runtime(env: Mapping[str, str] | None = None, runner: CommandRunner | None = None) -> RuntimePaths:
    values = os.environ if env is None else env
    execute = run_command if runner is None else runner
    system = platform.system()
    if system == "Linux":
        return _LinuxAdapter.detect(values, execute)
    if system == "Darwin":
        return _MacOSAdapter.detect(values, execute)
    raise RuntimeError(f"Unsupported platform: {system}. Sites Dashboard supports Linux and macOS.")


class ServiceManager:
    def __init__(self, runtime: RuntimePaths, runner: CommandRunner | None = None):
        self.runtime = runtime
        self.runner = run_command if runner is None else runner
        adapters = {"linux": _LinuxAdapter, "macos": _MacOSAdapter}
        self.adapter = adapters[runtime.platform]

    def status(self, service: str) -> str:
        return self.adapter.status(self.runtime, service, self.runner)

    def start(self, service: str, persistent: bool = True) -> CommandResult:
        return self.adapter.action(self.runtime, service, "start", persistent, self.runner)

    def stop(self, service: str) -> CommandResult:
        return self.adapter.action(self.runtime, service, "stop", False, self.runner)

    def restart(self, service: str, persistent: bool = True) -> CommandResult:
        return self.adapter.action(self.runtime, service, "restart", persistent, self.runner)

    def enable(self, service: str) -> CommandResult:
        return self.adapter.action(self.runtime, service, "enable", True, self.runner)
