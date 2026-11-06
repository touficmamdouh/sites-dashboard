# macOS Native Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add native macOS support to `sites-dashboard` through Homebrew while preserving the existing Linux CLI/TUI behavior and making installation, upgrades, and service management safe and understandable.

**Architecture:** Keep the existing CLI/TUI and site/backup/WordPress workflows, but move platform-specific paths, PHP discovery, service control, privilege execution, and ownership into a small adapter. Keep Linux on `apt`/`systemd`; use Homebrew and user `launchd` services on macOS for PHP-FPM, MariaDB, and Mailpit. Because the Homebrew Nginx formula defaults to port 8080 and user services cannot bind privileged ports, install a narrowly-scoped generated system `launchd` service for the Homebrew Nginx binary so the existing 80/443 URLs remain unchanged; do not use `sudo brew services`.

**Tech Stack:** Python 3, Bash, Typer, Rich, Textual, Homebrew, `brew services`, macOS `launchd`, Linux `systemd`, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-06-macos-native-support-design.md`

## Global Constraints

- "نطاق macOS هو macOS 13 أو أحدث، مع دعم Apple Silicon وIntel."
- "عند غياب Homebrew، يطلب المثبت موافقة واضحة قبل تشغيل المثبت الرسمي؛ وفي الوضع غير التفاعلي يتوقف برسالة تعليمات بدل التثبيت الصامت."
- "تعمل خدمات Homebrew بحساب المستخدم الحالي عبر `brew services`، ولا يُستخدم `www-data` على macOS." The Nginx 80/443 binding is the explicitly documented launchd exception described in Architecture.
- "نحافظ على عناوين المواقع الحالية على المنافذ 80/443، مع طلب صلاحية مرة واحدة؛ إذا كان المنفذ مستخدمًا أو غير متاح يظهر خطأ واضح ولا يحدث fallback صامت إلى منفذ آخر."
- "تبقى المواقع والنسخ والشهادات داخل مجلدات المستخدم وتستمر عبر الترقية، ولا تحذف إزالة الأداة بيانات المواقع افتراضيًا."
- "ندعم التثبيت الجديد والترقية من النسخة الحالية، ولا نعتبر نقل بيئة Linux كاملة إلى Mac ضمن الإصدار الأول."
- "يختار النظام PHP تلقائيًا، مع override عبر `SITES_PHP_SERVICE` عند وجود أكثر من إصدار."
- "يحدّث المثبت ملفات `.zprofile` و`.zshrc` عند الحاجة بطريقة idempotent، ولا يكرر الأسطر الموجودة."
- "`start` و`restart` يديران خدمات persistent عبر Homebrew لتبقى متاحة بعد إعادة تشغيل الجهاز."
- The existing command names remain unchanged: `list`, `check`, `services`, `enable`, `disable`, `start`, `stop`, `restart`, `create`, `backup`, `restore`, `snapshot`, `update_wordpress`, `update_all`, `ssl`, `export`, `fix_permissions`, and `dashboard`.
- Docker, Windows, a new GUI, and multi-version PHP switching are out of scope.

## Review Focus

- A Homebrew installation under `/opt/homebrew` and `/usr/local` must resolve the same runtime paths; pin this in Task 1's architecture tests.
- A user-selected PHP service must win over automatic discovery, while a missing service produces an actionable error; pin this in Task 1 and Task 2 tests.
- A port 80/443 collision must stop setup without changing to 8080/8443 silently; pin this in Task 4's installer tests.
- A pre-existing `/etc/hosts` line pointing the domain elsewhere must not be overwritten; pin this in Task 3's site-creation tests.
- Re-running install or upgrade must preserve user configuration and site data and must not duplicate shell/profile lines; pin this in Task 5's installer tests.

## File Map

- Create `bin/sites_platform.py`: pure platform/runtime detection plus `ServiceManager` and privilege abstractions shared by CLI and site creation.
- Create `bin/sites_config.py`: environment/state resolution for webroot, backup directory, Nginx paths, PHP selection, database credentials, and persisted runtime metadata.
- Modify `bin/sites`: replace Linux constants and direct service calls with `RuntimeConfig` and `ServiceManager`; preserve command names and TUI behavior.
- Modify `bin/newsite`: convert the shell helper to a Python executable using the shared platform/config modules; preserve `newsite DOMAIN [--wp]`.
- Modify `install.sh`: add macOS/Homebrew branch, venv setup, dependency checks, profile updates, readiness checks, and idempotent upgrade behavior; retain the Linux branch.
- Create `packaging/macos/com.sites-dashboard.nginx.plist.in`: minimal template for the privileged Nginx launchd service on ports 80/443.
- Modify `build.sh`: package shared Python modules and the converted `newsite` executable in the `.deb` staging area.
- Modify `DEBIAN/postinst` and `DEBIAN/prerm`: keep Debian lifecycle behavior isolated from macOS and stop purging unrelated system Python packages.
- Create `uninstall.sh`: remove installed binaries/services/profile entries while preserving sites, backups, certificates, and databases unless explicitly requested.
- Create `tests/test_sites_platform.py`, `tests/test_sites_config.py`, `tests/test_sites_cli_integration.py`, `tests/test_newsite.py`, and `tests/test_install.py`.
- Create `tests/conftest.py` for importing the executable modules from `bin/` under test.
- Create `requirements-dev.txt` with the test-only dependencies and `.github/workflows/ci.yml` for Linux plus macOS runners.
- Modify `README.md`: document macOS prerequisites, installation, ports, service lifecycle, upgrade/uninstall, troubleshooting, and the preserved Linux path.

### Task 1: Establish the platform and runtime contracts

**Files:**
- Create: `bin/sites_platform.py`
- Create: `bin/sites_config.py`
- Create: `tests/test_sites_platform.py`
- Create: `tests/test_sites_config.py`
- Create: `tests/conftest.py`
- Create: `requirements-dev.txt`

**Interfaces:**
- Consumes: environment variables, `platform.system()`, `platform.machine()`, `shutil.which`, `brew --prefix`, PHP service listings, and a test-injectable command runner.
- Produces: `RuntimePaths`, `RuntimeConfig`, `CommandResult`, `ServiceManager`, `detect_runtime(env: Mapping[str, str] | None = None, runner: CommandRunner | None = None) -> RuntimePaths`, and `load_config(env: Mapping[str, str] | None = None, runtime: RuntimePaths | None = None) -> RuntimeConfig`.

- [ ] **Step 1: Add the failing platform-detection tests**

  Add tests named `test_detect_macos_arm_uses_opt_homebrew`, `test_detect_macos_intel_uses_usr_local_homebrew`, `test_detect_linux_keeps_existing_nginx_paths`, `test_php_service_override_wins`, `test_missing_homebrew_has_actionable_error`, and `test_runtime_user_is_not_www_data_on_macos`. Assert the exact `brew_prefix`, Nginx paths, service backend, PHP service, socket, and runtime user for each fixture.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `python3 -m pytest tests/test_sites_platform.py tests/test_sites_config.py -q`

  Expected: FAIL because the shared modules and contracts do not yet exist.

- [ ] **Step 3: Implement `RuntimePaths`, `RuntimeConfig`, and `detect_runtime(...)`**

  Keep detection pure through an injected runner. Resolve Homebrew using `SITES_BREW_PREFIX`, then `brew --prefix`, then the known prefixes only as a diagnostic fallback; never assume one architecture. Resolve PHP using `SITES_PHP_SERVICE` first, then the active Homebrew/Linux service and socket. Keep all Linux defaults in the Linux adapter and all macOS defaults in the macOS adapter.

- [ ] **Step 4: Implement `ServiceManager` and config loading**

  Expose `status(service: str) -> str`, `start(service: str, persistent: bool = True) -> CommandResult`, `stop(service: str) -> CommandResult`, `restart(service: str, persistent: bool = True) -> CommandResult`, and `enable(service: str) -> CommandResult`. Map Linux to `systemctl`; map PHP-FPM, MariaDB, and Mailpit on macOS to `brew services`; route the Nginx port-80/443 service through the generated launchd label. `load_config` must preserve existing environment overrides and use `~/www` and `~/backups` only as dynamic defaults.

- [ ] **Step 5: Run the focused tests and verify they pass**

  Run: `python3 -m pytest tests/test_sites_platform.py tests/test_sites_config.py -q`

  Expected: PASS with no real service commands executed.

- [ ] **Step 6: Commit the contracts**

  ```bash
  git add bin/sites_platform.py bin/sites_config.py tests/test_sites_platform.py tests/test_sites_config.py requirements-dev.txt
  git commit -m "refactor: add cross-platform runtime contracts"
  ```

### Task 2: Migrate the CLI and TUI to the shared runtime

**Files:**
- Modify: `bin/sites`
- Create: `tests/test_sites_cli_integration.py`

**Interfaces:**
- Consumes: `RuntimeConfig` and `ServiceManager` from Task 1.
- Produces: the same Typer commands and `run_dashboard()` behavior, now driven by discovered paths and service names.

- [ ] **Step 1: Add failing integration tests around dynamic paths and services**

  Add tests named `test_get_all_sites_uses_runtime_nginx_paths`, `test_get_service_status_uses_discovered_php_service`, `test_start_uses_service_manager_for_all_runtime_services`, `test_restart_named_service_does_not_restart_hardcoded_php`, and `test_create_resolves_installed_newsite_path`. Use temporary directories and fake command runners; assert no call contains hardcoded `/run/php/php8.3-fpm.sock`, `systemctl` on macOS, or `~/.local/bin/newsite` when the binary is installed elsewhere.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `python3 -m pytest tests/test_sites_cli_integration.py -q`

  Expected: FAIL because `bin/sites` currently reads module-level Linux constants and hardcodes service names.

- [ ] **Step 3: Replace module-level paths and service names in `bin/sites`**

  Initialize `RUNTIME = load_config()` once at command startup, pass it to helpers that need test isolation, and replace `SITES_AVAILABLE`, `SITES_ENABLED`, `WEBROOT`, and `BACKUP_DIR` reads with `RUNTIME`. Replace `get_service_status`, `cli_start`, `cli_stop`, and `cli_restart` with `ServiceManager` calls. Keep output labels and command names stable.

- [ ] **Step 4: Make executable discovery and TUI refresh platform-neutral**

  Resolve `newsite` with `SITES_NEWSITE`, a sibling of the installed `sites` executable, then `shutil.which("newsite")`. Make the dashboard render the discovered service list and socket instead of `php8.3-fpm`/`php8.5-fpm` branches. Keep backup, restore, export, SSL, and WordPress operations on the shared config paths.

- [ ] **Step 5: Run the focused tests and a CLI help smoke test**

  Run: `python3 -m pytest tests/test_sites_cli_integration.py -q && python3 bin/sites --help`

  Expected: PASS; help lists every existing command and no service command is executed by `--help`.

- [ ] **Step 6: Commit the CLI migration**

  ```bash
  git add bin/sites tests/test_sites_cli_integration.py
  git commit -m "refactor: route CLI and TUI through runtime adapter"
  ```

### Task 3: Convert site creation to the shared Python runtime

**Files:**
- Modify: `bin/newsite`
- Create: `tests/test_newsite.py`

**Interfaces:**
- Consumes: `RuntimeConfig`, `ServiceManager`, and privilege runner from Task 1.
- Produces: executable `newsite` with `create_site(domain: str, wp: bool, config: RuntimeConfig, runner: CommandRunner) -> CreateResult` and unchanged CLI syntax `newsite DOMAIN [--wp]`.

- [ ] **Step 1: Add failing site-creation tests**

  Add tests named `test_plain_site_writes_platform_nginx_config`, `test_wordpress_site_uses_runtime_owner`, `test_existing_hosts_mapping_is_preserved`, `test_missing_hosts_mapping_is_added_once`, `test_database_failure_does_not_report_success`, and `test_nginx_port_collision_is_reported`. Use temporary config/webroot directories and fake `sudo`, MySQL, curl, and Nginx runners.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `python3 -m pytest tests/test_newsite.py -q`

  Expected: FAIL because the current helper is a Linux-only Bash script with hardcoded ownership, paths, and `sed -i` behavior.

- [ ] **Step 3: Rewrite `bin/newsite` as a Python executable**

  Preserve the existing usage and seven-step output shape, but move domain validation, directory creation, Nginx config rendering, `/etc/hosts` editing, database creation, WordPress download/config generation, ownership, and reload into testable functions. Use `runtime_user()`/`runtime_group()` instead of `www-data`; use `SITES_DB_ROOT_USER`/`SITES_DB_ROOT_PASS` without embedding credentials in shell command strings; use an atomic temporary config plus privileged install.

- [ ] **Step 4: Add collision-safe hosts and port behavior**

  Read `/etc/hosts` before editing. If the domain already maps to `127.0.0.1` or `::1`, leave it unchanged; if it maps elsewhere, fail with the conflicting line and do not overwrite it. Check 80/443 before generating the success message and return a diagnostic naming the process/port when unavailable.

- [ ] **Step 5: Run the focused tests and a dry CLI validation**

  Run: `python3 -m pytest tests/test_newsite.py -q && python3 bin/newsite --help`

  Expected: PASS; the help/usage output remains compatible and no real site is created by the test suite.

- [ ] **Step 6: Commit site creation**

  ```bash
  git add bin/newsite tests/test_newsite.py
  git commit -m "refactor: make site creation cross-platform"
  ```

### Task 4: Add the macOS service/bootstrap path

**Files:**
- Create: `packaging/macos/com.sites-dashboard.nginx.plist.in`
- Modify: `bin/sites_platform.py`
- Create: `tests/test_macos_services.py`

**Interfaces:**
- Consumes: `RuntimePaths`, `RuntimeConfig`, and the Homebrew prefix from Task 1.
- Produces: `render_nginx_launchd_plist(runtime: RuntimePaths, config_path: Path, log_dir: Path) -> str`, `install_nginx_service(...) -> CommandResult`, and service status/start/stop/restart operations for the Nginx launchd label.

- [ ] **Step 1: Add failing service/bootstrap tests**

  Add tests named `test_rendered_plist_uses_homebrew_nginx_and_current_user`, `test_nginx_service_is_system_launchd_not_sudo_brew_services`, `test_brew_services_are_user_scoped_for_php_mariadb_mailpit`, `test_port_collision_stops_install`, and `test_install_is_idempotent`. Assert exact label, executable, config path, user, log paths, and commands; assert that no test path calls `sudo brew services`.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `python3 -m pytest tests/test_macos_services.py -q`

  Expected: FAIL because no macOS service template or installer branch exists.

- [ ] **Step 3: Add the launchd template and renderer**

  The template must run the Homebrew Nginx binary with the generated config, write logs under the user-owned Sites Dashboard state directory, and configure Nginx workers to run as the current macOS user while the system launchd master retains the privilege required for ports 80/443. Installation must use `sudo launchctl bootstrap`/`bootout` only for this generated label, never `sudo brew services`, and must refuse to replace a non-owned plist or a conflicting listener.

- [ ] **Step 4: Implement persistent service actions**

  Use `brew services start|stop|restart|list` for PHP-FPM, MariaDB, and Mailpit under the logged-in user. Use the generated launchd label for Nginx. Return normalized `active`, `inactive`, `failed`, or `unknown` states to the CLI/TUI.

- [ ] **Step 5: Run the focused tests and verify they pass**

  Run: `python3 -m pytest tests/test_macos_services.py -q`

  Expected: PASS with all Homebrew, launchctl, sudo, and port probes mocked.

- [ ] **Step 6: Commit the macOS service path**

  ```bash
  git add packaging/macos/com.sites-dashboard.nginx.plist.in bin/sites_platform.py install.sh tests/test_macos_services.py
  git commit -m "feat: add macOS persistent service management"
  ```

### Task 5: Make installation, upgrade, packaging, and removal safe

**Files:**
- Modify: `install.sh`
- Modify: `build.sh`
- Modify: `DEBIAN/postinst`
- Modify: `DEBIAN/prerm`
- Create: `uninstall.sh`
- Create: `tests/test_install.py`

**Interfaces:**
- Consumes: the runtime/service contracts and macOS launchd path from Tasks 1 and 4.
- Produces: idempotent `install.sh`, Linux `.deb` packaging that includes all shared modules, and a data-preserving `uninstall.sh`.

- [ ] **Step 1: Add failing installer tests**

  Add tests named `test_macos_installer_requires_consent_for_missing_brew`, `test_noninteractive_missing_brew_prints_manual_steps`, `test_profile_update_is_idempotent`, `test_upgrade_preserves_runtime_state`, `test_deb_contains_shared_modules`, and `test_uninstall_preserves_sites_backups_and_certificates`. Stub all external commands and run the scripts in temporary HOME/PATH roots.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `python3 -m pytest tests/test_install.py -q`

  Expected: FAIL because `install.sh` only handles Debian and `build.sh` does not package shared modules.

- [ ] **Step 3: Implement the macOS installer branch**

  Detect `Darwin`, verify macOS 13+, detect architecture, check Homebrew, prompt before invoking the official Homebrew installer when interactive, and otherwise print exact manual steps. Install/verify Python, Nginx, PHP, MariaDB, Mailpit, mkcert, curl, and wget through Homebrew; create an application-owned virtualenv for Typer/Rich/Textual; write runtime metadata under `~/.config/sites-dashboard`; install binaries and modules to one discoverable prefix.

- [ ] **Step 4: Add readiness checks and idempotent profile/service setup**

  Check that PHP-FPM, MariaDB, Mailpit, and Nginx are reachable before reporting success. Update `.zprofile` and `.zshrc` only when needed, preserve user edits, and make repeated install/upgrade runs converge on the same state. Keep Linux installation behavior isolated and do not make Debian packages install macOS files.

- [ ] **Step 5: Fix package lifecycle and removal behavior**

  Extend `build.sh` to copy `sites_platform.py`, `sites_config.py`, and the Python `newsite` executable. Make Debian lifecycle scripts remove only files owned by this package and stop purging unrelated Python packages. Make `uninstall.sh` stop/remove owned services and profile entries while preserving sites, backups, certificates, and databases by default; require an explicit flag before deleting data.

- [ ] **Step 6: Run installer tests and package smoke tests**

  Run: `python3 -m pytest tests/test_install.py -q && ./build.sh 1.3.0 && dpkg-deb -c sites-dashboard_1.3.0_all.deb`

  Expected: PASS; the package contains the shared runtime files, no macOS installer files are installed by the `.deb`, and the generated package lists only expected paths.

- [ ] **Step 7: Commit installation and lifecycle changes**

  ```bash
  git add install.sh build.sh DEBIAN/postinst DEBIAN/prerm uninstall.sh tests/test_install.py
  git commit -m "feat: add safe cross-platform installation lifecycle"
  ```

### Task 6: Documentation, CI, and end-to-end verification

**Files:**
- Modify: `README.md`
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_release_smoke.py`

**Interfaces:**
- Consumes: the completed installer, runtime adapter, and CLI from Tasks 1–5.
- Produces: documented installation/upgrade/uninstall flows and automated cross-platform checks.

- [ ] **Step 1: Add failing release-smoke assertions**

  Add tests named `test_help_lists_existing_commands`, `test_config_defaults_are_user_owned`, and `test_source_tree_has_no_unscoped_linux_assumptions`. The last test may allow Linux adapter files and tests but should flag hardcoded Linux paths/service names in shared macOS code.

- [ ] **Step 2: Run the focused tests and verify they fail where documentation/CI work is absent**

  Run: `python3 -m pytest tests/test_release_smoke.py -q`

  Expected: FAIL until the final command surface and source layout checks are complete.

- [ ] **Step 3: Update the README in Arabic and English**

  Document macOS 13+, Apple Silicon/Intel, Homebrew consent, one-command install, first run, `http://domain.test`, HTTPS/`mkcert`, Mailpit, service commands, PHP override, upgrade, uninstall, port conflicts, and data preservation. Keep the Debian `.deb` path and current command reference intact.

- [ ] **Step 4: Add GitHub Actions coverage**

  Run unit/integration tests on `ubuntu-latest`, `macos-13`, and `macos-14` without touching the user machine’s real `/etc/hosts` or service state. Add a separate opt-in smoke job for installing Homebrew formulae and exercising service discovery; keep it non-destructive and bounded.

- [ ] **Step 5: Run the complete local verification suite**

  Run: `python3 -m pytest -q && bash -n install.sh build.sh uninstall.sh && python3 bin/sites --help && python3 bin/newsite --help`

  Expected: all tests pass, shell syntax checks pass, and both entry points show help without mutating the system.

- [ ] **Step 6: Commit documentation and CI**

  ```bash
  git add README.md .github/workflows/ci.yml tests/test_release_smoke.py
  git commit -m "docs: document macOS installation and add CI coverage"
  ```

## Final Verification Checklist

- [ ] Fresh macOS 13+ install succeeds on Apple Silicon.
- [ ] Fresh macOS 13+ install succeeds on Intel.
- [ ] Missing Homebrew requires explicit consent and gives manual instructions in non-interactive mode.
- [ ] Re-running install does not duplicate shell lines, overwrite user config, or delete sites/backups.
- [ ] Nginx serves `http://domain.test` and `https://domain.test` on ports 80/443, or fails with an explicit port-conflict message.
- [ ] PHP-FPM, MariaDB, and Mailpit persist across login/reboot through user services.
- [ ] Plain site and WordPress site creation work with macOS ownership and paths.
- [ ] Backup, restore, SSL, WordPress update, export, and TUI service status use discovered paths/services.
- [ ] Existing Linux commands and `.deb` installation remain functional.
- [ ] Uninstall removes owned runtime/service files but preserves user data by default.

## External Implementation References

- Homebrew service scope and persistence: [Homebrew brew services documentation](https://docs.brew.sh/Manpage.html#services).
- Homebrew Nginx service and its default non-privileged port: [Homebrew Nginx formula](https://formulae.brew.sh/formula/nginx).

