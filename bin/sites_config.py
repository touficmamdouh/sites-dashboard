"""Environment configuration shared by the CLI and site creation helper."""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sites_platform import RuntimePaths, apply_runtime_overrides, detect_runtime, expand_path


@dataclass(frozen=True)
class RuntimeConfig:
    runtime: RuntimePaths
    webroot: Path
    backup_dir: Path
    web_user: str
    web_group: str
    db_root_user: str = "root"
    db_root_pass: str = field(default="root", repr=False)


def load_config(env: Mapping[str, str] | None = None, runtime: RuntimePaths | None = None) -> RuntimeConfig:
    values = os.environ if env is None else env
    paths = detect_runtime(values) if runtime is None else apply_runtime_overrides(runtime, values)
    owner = values.get("SITES_WEB_USER") or values.get("SUDO_USER") or values.get("USER") or getpass.getuser()
    return RuntimeConfig(
        runtime=paths,
        webroot=expand_path(values.get("SITES_WEBROOT") or "~/www", values),
        backup_dir=expand_path(values.get("SITES_BACKUP_DIR") or "~/backups", values),
        web_user=owner,
        web_group=values.get("SITES_WEB_GROUP") or paths.runtime_group,
        db_root_user=values.get("SITES_DB_ROOT_USER") or "root",
        db_root_pass=values.get("SITES_DB_ROOT_PASS", "root"),
    )
