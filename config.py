"""
config.py
─────────
Single source of truth for the automated monitoring service.

Precedence (highest wins):
    1. Values saved via the UI / API   (monitor_config.json)
    2. Environment variables
    3. Built-in defaults below

Nothing about timing, paths, or queue size is hardcoded into the
service logic — everything is read from here and can be changed at
runtime through POST /api/config without editing source.

NETWORK LOCATION SUPPORT:
    source_type: "local" | "network" | "ftp" | "sftp" | "cloud"
    
    For "network":
        source_path: UNC path (e.g. \\192.168.0.3\Team Share\Folder)
        network_username: domain\username or username
        network_password: (encrypted in storage, never in JSON)
        network_domain: optional domain for authentication
"""

from __future__ import annotations

import os
import json
import threading
from pathlib import Path, PureWindowsPath
from dataclasses import dataclass, asdict, field
from typing import Optional

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "monitor_config.json"


def _env(name: str, default):
    """Read an env var, falling back to a typed default."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


def _validate_unc_path(path: str) -> bool:
    """Validate UNC path format (\\server\share\path)."""
    if not path:
        return False
    path = path.strip()
    # Must start with \\ and contain at least server and share
    if not path.startswith("\\\\"):
        return False
    parts = path.split("\\")
    # Format: ['', '', 'server', 'share', ...]
    return len(parts) >= 4 and parts[2] and parts[3]


def _validate_local_path(path: str) -> bool:
    """Validate local path format."""
    if not path:
        return False
    try:
        p = Path(path)
        return True
    except (ValueError, TypeError):
        return False


@dataclass
class MonitorConfig:
    # ── Source location ────────────────────────────────────────────────
    # source_type drives which provider in source_providers.py is used.
    source_type: str = _env("SOURCE_TYPE", "local")          # local | network | ftp | sftp | cloud
    source_path: str = _env("SOURCE_PATH", r"C:\Users\shyamkumar\Desktop\Test")
    recursive:   bool = _env("SOURCE_RECURSIVE", True)

    # ── Network Location Settings ──────────────────────────────────────
    # Used only when source_type == "network"
    network_username: str = _env("NETWORK_USERNAME", "")
    network_domain: str = _env("NETWORK_DOMAIN", "")
    # network_password is NEVER stored in JSON — only in encrypted session storage
    # See network_auth.py for credential handling

    # FTP / SFTP / cloud connection details (used only by their providers)
    remote_host: str = _env("SOURCE_HOST", "")
    remote_user: str = _env("SOURCE_USER", "")
    remote_pass: str = _env("SOURCE_PASS", "")
    remote_port: int = _env("SOURCE_PORT", 21)
    remote_dir:  str = _env("SOURCE_REMOTE_DIR", "/")

    # ── Schedule ───────────────────────────────────────────────────────
    # schedule_mode:
    #   "interval"  -> scan every scan_interval_seconds
    #   "window"    -> only scan between window_start and window_end (HH:MM),
    #                  still every scan_interval_seconds inside the window
    schedule_mode:        str = _env("SCHEDULE_MODE", "interval")
    scan_interval_seconds: int = _env("SCAN_INTERVAL_SECONDS", 120)   # 2 minutes
    window_start:          str = _env("WINDOW_START", "20:00")        # 8 PM
    window_end:            str = _env("WINDOW_END",   "08:00")        # 8 AM

    # Real-time watchdog observer (instant pickup) on top of the schedule.
    enable_watchdog: bool = _env("ENABLE_WATCHDOG", True)

    # ── Queue management ───────────────────────────────────────────────
    queue_size:  int = _env("QUEUE_SIZE", 5)        # PDFs per queue
    max_workers: int = _env("MAX_WORKERS", 1)       # parallel queue workers

    # ── Pipeline mode ──────────────────────────────────────────────────
    # If no Groq key is present the extractor falls back to free/mock mode.
    groq_api_key:      str = _env("GROQ_API_KEY", "")
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")

    # ── Auto-start the monitor when the Flask app boots ────────────────
    autostart: bool = _env("MONITOR_AUTOSTART", False)

    def validate(self) -> tuple[bool, str]:
        """
        Validate configuration.
        Returns (is_valid, error_message)
        """
        if self.source_type == "local":
            if not _validate_local_path(self.source_path):
                return False, f"Invalid local path: {self.source_path}"
        elif self.source_type == "network":
            if not _validate_unc_path(self.source_path):
                return False, f"Invalid UNC path format. Expected: \\\\server\\share\\path"
            if not self.network_username:
                return False, "Network username required for network locations"
        elif self.source_type in ("ftp", "sftp"):
            if not self.remote_host:
                return False, f"{self.source_type.upper()} host required"
        
        return True, ""

    def as_public_dict(self) -> dict:
        """Config for the UI — secrets masked."""
        d = asdict(self)
        for secret in ("remote_pass", "groq_api_key", "anthropic_api_key"):
            d[secret] = "********" if d.get(secret) else ""
        
        # Network password is NEVER included in any response
        d.pop("network_password", None)
        
        # Mask username if sensitive
        if d.get("network_username"):
            d["network_username"] = d["network_username"]  # shown for reference
        
        return d

    def as_dict_for_persistence(self) -> dict:
        """
        Dict for saving to JSON — excludes passwords.
        Network password is handled separately in secure storage.
        """
        d = asdict(self)
        # Never persist passwords to disk
        d.pop("remote_pass", None)
        d.pop("network_password", None)
        return d


# ── Thread-safe singleton with JSON persistence ────────────────────────

_lock     = threading.Lock()
_instance: MonitorConfig | None = None

# Fields the UI is allowed to change at runtime.
_EDITABLE = {
    "source_type", "source_path", "recursive",
    "network_username", "network_domain",
    "remote_host", "remote_user", "remote_pass", "remote_port", "remote_dir",
    "schedule_mode", "scan_interval_seconds", "window_start", "window_end",
    "enable_watchdog", "queue_size", "max_workers", "autostart",
}


def _load_from_disk() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_config() -> MonitorConfig:
    """Return the live config singleton (env defaults + saved overrides)."""
    global _instance
    with _lock:
        if _instance is None:
            cfg = MonitorConfig()
            for k, v in _load_from_disk().items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            _instance = cfg
        return _instance


def update_config(changes: dict, network_password: str | None = None) -> tuple[MonitorConfig, str]:
    """
    Apply UI changes, persist non-secret editable fields, return new config.
    
    Args:
        changes: dict of config changes
        network_password: optional new network password (handled separately)
    
    Returns:
        (updated_config, error_message or "")
    """
    global _instance
    with _lock:
        cfg = _instance or MonitorConfig()
        
        # Validate source_type before applying changes
        source_type = changes.get("source_type", cfg.source_type)
        if source_type == "network" and changes.get("source_path"):
            if not _validate_unc_path(changes["source_path"]):
                return cfg, "Invalid UNC path format. Expected: \\\\server\\share\\path"
        
        for k, v in changes.items():
            if k in _EDITABLE and hasattr(cfg, k):
                # Keep the declared type
                current = getattr(cfg, k)
                if isinstance(current, bool):
                    v = bool(v)
                elif isinstance(current, int):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                setattr(cfg, k, v)
        
        # Validate the updated config
        is_valid, error = cfg.validate()
        if not is_valid:
            return cfg, error
        
        _instance = cfg

        # Persist only editable, non-secret fields.
        to_save = {k: getattr(cfg, k) for k in _EDITABLE}
        # Remove password fields from persistence
        to_save.pop("network_password", None)
        
        CONFIG_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
        
        # Handle network password separately (encrypted storage)
        if source_type == "network" and network_password:
            from network_auth import store_network_credentials
            store_network_credentials(cfg.network_username, network_password, cfg.network_domain)
        
        return cfg, ""


def reset_config() -> None:
    """Reset configuration to defaults."""
    global _instance
    with _lock:
        _instance = None
        CONFIG_FILE.unlink(missing_ok=True)
