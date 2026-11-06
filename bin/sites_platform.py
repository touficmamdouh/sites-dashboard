"""Platform discovery and service commands, without import-time side effects."""

from __future__ import annotations

import getpass
import grp
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING
from uuid import uuid4
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from sites_config import RuntimeConfig


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


NGINX_LAUNCHD_LABEL = "com.sites-dashboard.nginx"
NGINX_PLIST_TEMPLATE = Path(__file__).resolve().parents[1] / "packaging/macos/com.sites-dashboard.nginx.plist.in"


def _nginx_service_paths(runtime: RuntimePaths) -> tuple[Path, Path]:
    if (runtime.platform != "macos" or runtime.service_backend != "launchd"
            or runtime.nginx_launchd_label != NGINX_LAUNCHD_LABEL
            or runtime.nginx_launchd_plist is None or not runtime.nginx_launchd_plist.is_absolute()
            or runtime.nginx_launchd_plist.name != f"{NGINX_LAUNCHD_LABEL}.plist"
            or runtime.brew_prefix is None or not runtime.brew_prefix.is_absolute()):
        raise ValueError("Nginx bootstrap requires the generated macOS launchd label, plist, and Homebrew prefix.")
    return runtime.brew_prefix / "bin/nginx", runtime.nginx_launchd_plist


def render_nginx_launchd_plist(runtime: RuntimePaths, config_path: Path, log_dir: Path) -> str:
    nginx, _ = _nginx_service_paths(runtime)
    for account in (runtime.runtime_user, runtime.runtime_group):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", account) or account == "root":
            raise ValueError("Nginx workers require a non-root macOS user and group.")
    if any(not path.is_absolute() or any(ord(char) < 32 for char in str(path))
           for path in (nginx, config_path, log_dir)):
        raise ValueError("Nginx config and log paths must be absolute and contain no control characters.")
    return Template(NGINX_PLIST_TEMPLATE.read_text()).substitute({
        key: escape(str(value)) for key, value in {
            "owner": runtime.runtime_user, "group": runtime.runtime_group,
            "nginx": nginx, "config": config_path,
            "stdout": log_dir / "nginx.stdout.log", "stderr": log_dir / "nginx.stderr.log",
        }.items()
    })


def _owned_nginx_plist(runtime: RuntimePaths) -> bytes | None:
    _, destination = _nginx_service_paths(runtime)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Refusing non-owned Nginx plist: {destination} is not a regular file.")
    content = destination.read_bytes()
    try:
        plist = plistlib.loads(content)
    except Exception as error:
        # plistlib's XML/binary decoders have no common malformed-input exception.
        raise ValueError(f"Refusing non-owned Nginx plist: {destination} is malformed.") from error
    try:
        arguments = plist["ProgramArguments"]
        old_config = Path(arguments[2])
        old_logs = Path(plist["StandardErrorPath"]).parent
        expected = plistlib.loads(render_nginx_launchd_plist(runtime, old_config, old_logs).encode())
        if plist == expected:
            return content
    except (ValueError, TypeError, KeyError, IndexError):
        pass
    raise ValueError(f"Refusing to replace non-owned Nginx plist: {destination}. Inspect it before retrying.")


def _launchd_state(result: CommandResult) -> str:
    if result.returncode:
        return "inactive" if "Could not find service" in result.stderr else "unknown"
    if re.search(r"(?m)^\s*state\s*=\s*running\s*$", result.stdout):
        return "active"
    exit_code = re.search(r"(?m)^\s*last exit code\s*=\s*(-?\d+)\s*$", result.stdout)
    if exit_code and int(exit_code[1]):
        return "failed"
    return "inactive" if re.search(r"(?m)^\s*state\s*=\s*(?:waiting|exited|not running)\s*$", result.stdout) else "unknown"


def _nginx_job_loaded(runtime: RuntimePaths, job: CommandResult, owned: bytes | None) -> bool:
    if _launchd_state(job) == "unknown":
        raise ValueError(f"Cannot inspect Nginx launchd job: {job.stderr or job.stdout}")
    loaded = job.returncode == 0
    if loaded and (owned is None or not any(
            line.strip() == f"path = {runtime.nginx_launchd_plist}" for line in job.stdout.splitlines())):
        raise ValueError("Refusing non-owned loaded Nginx launchd job; its plist path does not match.")
    return loaded


def _nginx_launchd_action(runtime: RuntimePaths, action: str, persistent: bool, runner: CommandRunner) -> CommandResult:
    try:
        _nginx_service_paths(runtime)
        owned = _owned_nginx_plist(runtime)
        if owned is None:
            raise ValueError(f"Missing owned Nginx plist: {runtime.nginx_launchd_plist}. Install the service first.")
        target = f"system/{runtime.nginx_launchd_label}"
        _nginx_job_loaded(runtime, runner(["launchctl", "print", target]), owned)
        commands = {
            "start": ["bootstrap", "system", str(runtime.nginx_launchd_plist)],
            "restart": ["kickstart", "-k", target],
            "stop": ["bootout", target],
            "enable": ["enable", target],
        }
        actions = [commands[action]]
        if action in {"start", "restart"} and persistent:
            actions.insert(0, ["enable", target])
        for command in actions:
            if _owned_nginx_plist(runtime) != owned:
                raise ValueError("Nginx plist changed before service action; refusing mutation.")
            result = runner(["sudo", "-n", "launchctl", *command])
            if result.returncode:
                return result
        return result
    except (OSError, ValueError) as error:
        return CommandResult(stderr=str(error), returncode=1)


def _nginx_port_check(runtime: RuntimePaths, job: CommandResult, runner: CommandRunner) -> CommandResult:
    result = runner(["sudo", "-n", "lsof", "-nP", "+c", "0", "-iTCP:80", "-iTCP:443", "-sTCP:LISTEN", "-Fpcn"])
    if result.returncode not in {0, 1}:
        return CommandResult(stderr=f"Cannot inspect ports 80/443: {result.stderr}", returncode=1)
    listeners = []
    pid, name = "", ""
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid, name = line[1:], ""
        elif line.startswith("c"):
            name = line[1:]
        elif line.startswith("n"):
            listeners.append((name, pid, line.rsplit(":", 1)[-1]))
    valid = bool(listeners) and all(name and re.fullmatch(r"[1-9][0-9]*", pid) and port in {"80", "443"}
                                    for name, pid, port in listeners)
    warning = result.stderr.strip()
    if ((result.stdout.strip() and not valid) or (warning and not (
            result.returncode == 1 and valid and all(
                re.fullmatch(r"lsof: (?:WARNING: )?Internet address not located: TCP:(?:80|443)", line.strip())
                for line in warning.splitlines() if line.strip())))):
        return CommandResult(stderr=f"Cannot inspect ports 80/443: {warning or 'unrecognized listener information'}", returncode=1)
    owned = set()
    if any(name == "nginx" for name, _, _ in listeners) and _launchd_state(job) == "active":
        pids = re.findall(r"(?m)^\s*pid\s*=\s*([1-9][0-9]*)\s*$", job.stdout)
        if len(pids) == 1:
            processes = runner(["ps", "-axo", "pid=,ppid=,comm="])
            parents = {}
            if not processes.returncode:
                for line in processes.stdout.splitlines():
                    fields = line.split(None, 2)
                    if (len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit()
                            and Path(fields[2].split()[0]).name.rstrip(":") == "nginx"):
                        parents[fields[0]] = fields[1]
                if pids[0] in parents:
                    owned.add(pids[0])
                    while children := {pid for pid, parent in parents.items() if parent in owned} - owned:
                        owned.update(children)
    for name, pid, port in listeners:
        if name != "nginx" or pid not in owned:
            return CommandResult(stderr=f"Port {port} is unavailable: {name} (PID {pid}). Cannot verify managed Nginx ownership.", returncode=1)
    return CommandResult(stderr=warning)


def install_nginx_service(config: RuntimeConfig, config_path: Path, log_dir: Path,
                          runner: CommandRunner | None = None) -> CommandResult:
    """Install the owned Nginx LaunchDaemon; callers generate its nginx.conf first.

    The config must omit main-context user/daemon directives, supplied through
    -g here, and include the runtime's enabled sites. Run as the login user with
    sudo available for the narrowly scoped system commands.
    """
    execute = run_command if runner is None else runner
    runtime = config.runtime
    try:
        if os.geteuid() == 0:
            raise ValueError("Run Nginx service installation as the logged-in user, with sudo access.")
        nginx, destination = _nginx_service_paths(runtime)
        rendered = render_nginx_launchd_plist(runtime, config_path, log_dir).encode()
        previous = _owned_nginx_plist(runtime)
        target = f"system/{runtime.nginx_launchd_label}"
        job = execute(["launchctl", "print", target])
        loaded = _nginx_job_loaded(runtime, job, previous)
        ports = _nginx_port_check(runtime, job, execute)
        if ports.returncode:
            return ports
        log_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        if log_dir.is_symlink() or log_dir.stat().st_uid != os.geteuid():
            raise ValueError(f"Nginx log directory must be owned by the logged-in user: {log_dir}")
        result = execute(["sudo", "-n", str(nginx), "-t", "-c", str(config_path),
                          "-g", f"daemon off; user {runtime.runtime_user} {runtime.runtime_group};"])
        if result.returncode:
            return result
        changed = previous != rendered
        if changed:
            staged = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            with tempfile.TemporaryDirectory(prefix="sites-nginx-") as temporary:
                source = Path(temporary) / "nginx.plist"
                source.write_bytes(rendered)
                try:
                    result = execute(["sudo", "-n", "install", "-o", "root", "-g", "wheel", "-m", "0644", str(source), str(staged)])
                    if result.returncode:
                        return result
                    if _owned_nginx_plist(runtime) != previous:
                        raise ValueError("Nginx plist changed during installation; refusing to replace it.")
                    if loaded:
                        result = execute(["sudo", "-n", "launchctl", "bootout", target])
                        if result.returncode:
                            return result
                        loaded = False
                    # ln publishes new files without replacing a concurrent install.
                    command = ["ln", str(staged), str(destination)] if previous is None else ["mv", "-f", str(staged), str(destination)]
                    result = execute(["sudo", "-n", *command])
                    if result.returncode:
                        return result
                finally:
                    execute(["sudo", "-n", "rm", "-f", str(staged)])
        result = execute(["sudo", "-n", "launchctl", "enable", target])
        if result.returncode:
            return result
        if not loaded:
            result = execute(["sudo", "-n", "launchctl", "bootstrap", "system", str(destination)])
        elif _launchd_state(job) != "active":
            result = execute(["sudo", "-n", "launchctl", "kickstart", "-k", target])
        return result if result.returncode else replace(result, stderr="\n".join(filter(None, [ports.stderr, result.stderr])))
    except (OSError, ValueError) as error:
        return CommandResult(stderr=str(error), returncode=1)


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


def _socket_from_dump(result: CommandResult, service: str) -> Path:
    # php-fpm writes its configuration dump to stderr, including notices.
    listeners = re.findall(r"(?m)\blisten\s*=\s*([^\n]+)", result.stdout + "\n" + result.stderr)
    listeners = [value.strip() for value in listeners]
    sockets = set(value for value in listeners if value.startswith("/"))
    if result.returncode or len(sockets) != 1 or any(not value.startswith("/") for value in listeners):
        raise RuntimeError(
            f"Cannot resolve a single PHP-FPM Unix socket for {service}. "
            "Configure the PHP-FPM pool listen path or set SITES_PHP_SOCK explicitly."
        )
    return Path(sockets.pop())


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
        if not env.get("SITES_PHP_SOCK"):
            result = runner([str(binary), "-tt"])
            if not result.returncode:
                socket = _socket_from_dump(result, service)
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
            socket = str(_socket_from_dump(result, service))
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
            return _launchd_state(result)
        if os.geteuid() == 0:
            return "unknown"
        try:
            services = _brew_services(runtime.brew_binary, runner)
        except RuntimeError:
            return "unknown"
        state = next((item.get("status") for item in services if item.get("name") == service), None)
        return {"started": "active", "stopped": "inactive", "none": "inactive", "error": "failed"}.get(state, "unknown")

    @staticmethod
    def action(runtime: RuntimePaths, service: str, action: str, persistent: bool, runner: CommandRunner) -> CommandResult:
        if service == "nginx":
            return _nginx_launchd_action(runtime, action, persistent, runner)
        if os.geteuid() == 0:
            return CommandResult(stderr="Run Homebrew services as the logged-in user, without sudo.", returncode=1)
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
