"""Resolved filesystem paths for the setup engine.

Every path the setup engine creates, reads, or reverts is derived here from a single
``data_dir()`` so that ``TOKDASH_DATA_DIR`` redirects *all* state — manifest, config,
usage DB, the managed runtime tree, the runtime ownership marker, and the generated
service ``ExecStart`` — into one place (plan §13). ``~/.tokdash`` is only the default.

The resolution of ``data_dir()`` deliberately matches ``usage_store.usage_db_path``
(``usage_store.py``) so the service reads/writes the same state the manifest records.
"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Resolved Tokdash data dir: ``$TOKDASH_DATA_DIR`` if set, else ``~/.tokdash``."""
    return Path(os.environ.get("TOKDASH_DATA_DIR", "~/.tokdash")).expanduser()


def is_default_data_dir() -> bool:
    """True when no ``TOKDASH_DATA_DIR`` override is in effect.

    The systemd unit only needs an ``Environment=TOKDASH_DATA_DIR=`` line when the
    data dir is non-default (§10.1); for the default we keep the unit clean.
    """
    raw = os.environ.get("TOKDASH_DATA_DIR", "").strip()
    return not raw


def manifest_path() -> Path:
    """The revert manifest (``install.json``) written by setup, read by doctor/uninstall."""
    return data_dir() / "install.json"


def config_path() -> Path:
    return data_dir() / "config.json"


def pricing_db_override_path() -> Path:
    """User pricing-DB overrides, under the data dir (survives `tokdash update` / pip reinstall).

    The packaged ``pricing_db.json`` is the read-only baseline shipped in site-packages;
    edits made via the dashboard are written here and FULLY REPLACE the baseline at load
    (WYSIWYG editor semantics — a deleted model stays deleted), so a pip/pipx reinstall can't
    wipe them and a read-only install can't 500 on a write. A missing/corrupt override falls
    back to the baseline.
    """
    return data_dir() / "pricing_db.json"


def usage_db_path() -> Path:
    """Where the usage DB lives — for reporting/uninstall only (kept unless ``--purge``)."""
    explicit = os.environ.get("TOKDASH_USAGE_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return data_dir() / "usage.sqlite3"


def runtime_dir() -> Path:
    """Root of the setup-owned runtime tree (managed venv / future binary)."""
    return data_dir() / "runtime"


def managed_venv_dir() -> Path:
    return runtime_dir() / "python-venv"


def managed_venv_python() -> Path:
    # Windows venvs put the interpreter under Scripts/, but Phase 1 targets POSIX.
    return managed_venv_dir() / "bin" / "python"


def runtime_marker_path() -> Path:
    """Ownership marker proving setup created the runtime tree (gates removal, §12.3)."""
    return runtime_dir() / ".tokdash-managed"


def systemd_user_dir() -> Path:
    """User-unit dir where setup writes its service file.

    ``XDG_CONFIG_HOME`` redirects the file target, but an already-running systemd user
    manager may still have a same-named unit loaded from another path. Setup verifies
    systemd's loaded FragmentPath after starting the service before reporting success.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path("~/.config").expanduser()
    return base / "systemd" / "user"


def systemd_unit_path(name: str = "tokdash") -> Path:
    return systemd_user_dir() / f"{name}.service"


def launchd_plist_path() -> Path:
    """macOS LaunchAgent path (Phase 4; defined here so doctor can report it)."""
    return Path("~/Library/LaunchAgents/com.tokdash.tokdash.plist").expanduser()
