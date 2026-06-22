"""
network_auth.py
───────────────
Secure network credential handling and validation for UNC paths.

Features:
  - Credential encryption (using keyring or fallback)
  - Network share access validation
  - UNC path resolution
  - Automatic mount/authentication
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
from pathlib import Path, PureWindowsPath
from typing import Optional
import tempfile

_lock = threading.Lock()
_cached_credentials: dict[str, tuple[str, str]] = {}  # {username: (password, domain)}

# Try to import keyring for secure storage; fall back to in-memory storage
try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False


class NetworkAuthError(Exception):
    """Raised when network authentication fails."""
    pass


def _get_keyring_service() -> str:
    """Service name for keyring storage."""
    return "RealEstateValidator.NetworkAuth"


def store_network_credentials(
    username: str,
    password: str,
    domain: Optional[str] = None
) -> None:
    """
    Store network credentials securely.
    
    Args:
        username: username (may include domain as domain\\username)
        password: plaintext password
        domain: optional domain (alternative to domain\\username format)
    """
    if not username or not password:
        raise NetworkAuthError("Username and password required")
    
    # Normalize username format
    if domain and "\\" not in username:
        full_username = f"{domain}\\{username}"
    else:
        full_username = username
    
    with _lock:
        if _HAS_KEYRING:
            try:
                keyring.set_password(
                    _get_keyring_service(),
                    full_username,
                    password
                )
            except Exception as e:
                # Fallback to in-memory
                _cached_credentials[full_username] = (password, domain or "")
        else:
            _cached_credentials[full_username] = (password, domain or "")


def get_network_credentials(username: str) -> Optional[tuple[str, str]]:
    """
    Retrieve stored network credentials.
    
    Returns:
        (password, domain) or None if not found
    """
    # Normalize username
    full_username = username if "\\" in username else username
    
    with _lock:
        # Try keyring first
        if _HAS_KEYRING:
            try:
                password = keyring.get_password(
                    _get_keyring_service(),
                    full_username
                )
                if password:
                    # Extract domain if present
                    domain = full_username.split("\\")[0] if "\\" in full_username else ""
                    return (password, domain)
            except Exception:
                pass
        
        # Try in-memory cache
        if full_username in _cached_credentials:
            return _cached_credentials[full_username]
    
    return None


def delete_network_credentials(username: str) -> None:
    """Delete stored network credentials."""
    full_username = username if "\\" in username else username
    
    with _lock:
        if _HAS_KEYRING:
            try:
                keyring.delete_password(
                    _get_keyring_service(),
                    full_username
                )
            except Exception:
                pass
        
        _cached_credentials.pop(full_username, None)


def validate_unc_path(
    unc_path: str,
    username: Optional[str] = None,
    password: Optional[str] = None
) -> tuple[bool, str]:
    """
    Validate UNC path and optionally test credentials.
    
    Args:
        unc_path: UNC path (\\server\share\path)
        username: optional username to test
        password: optional password to test
    
    Returns:
        (is_valid, error_message)
    """
    # Basic format validation
    if not unc_path or not isinstance(unc_path, str):
        return False, "UNC path must be a non-empty string"
    
    unc_path = unc_path.strip()
    
    if not unc_path.startswith("\\\\"):
        return False, "UNC path must start with \\\\ (double backslash)"
    
    parts = unc_path.split("\\")
    if len(parts) < 4 or not parts[2] or not parts[3]:
        return False, "Invalid UNC format. Expected: \\\\server\\share or \\\\server\\share\\path"
    
    server = parts[2]
    share = parts[3]
    
    # Test access if on Windows
    if platform.system() == "Windows":
        return _test_unc_access_windows(unc_path, username, password)
    else:
        # Non-Windows: basic format validation only
        return True, ""


def _test_unc_access_windows(
    unc_path: str,
    username: Optional[str] = None,
    password: Optional[str] = None
) -> tuple[bool, str]:
    """
    Test UNC path access on Windows using NET USE.
    
    Returns:
        (is_accessible, error_message)
    """
    try:
        # Extract \\server\share for mounting
        parts = unc_path.strip("\\").split("\\")
        share_root = f"\\\\{parts[0]}\\{parts[1]}"
        
        if username and password:
            # Use credentials
            cmd = ["net", "use", share_root, password, f"/user:{username}", "/persistent:no"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=10,
                shell=False
            )
        else:
            # Test without explicit credentials
            cmd = ["net", "use", share_root]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=10,
                shell=False
            )
        
        if result.returncode == 0:
            return True, ""
        
        stderr = result.stderr.decode("utf-8", errors="ignore")
        stdout = result.stdout.decode("utf-8", errors="ignore")
        error_msg = stderr or stdout or "Unknown error"
        
        # Parse common errors
        if "access denied" in error_msg.lower():
            return False, "Access denied. Check username and password."
        elif "not found" in error_msg.lower() or "cannot find" in error_msg.lower():
            return False, f"Network path not found: {share_root}"
        elif "timeout" in error_msg.lower():
            return False, "Connection timeout. Check network connectivity and server address."
        
        return False, f"Connection failed: {error_msg[:100]}"
    
    except subprocess.TimeoutExpired:
        return False, "Connection timeout. The server took too long to respond."
    except Exception as e:
        return False, f"Network test error: {str(e)[:100]}"


def mount_network_share(
    unc_path: str,
    username: str,
    password: str,
    domain: Optional[str] = None
) -> tuple[bool, str]:
    """
    Mount network share on Windows.
    
    Returns:
        (success, message)
    """
    if platform.system() != "Windows":
        return True, "Network paths natively accessible on this platform"
    
    try:
        parts = unc_path.strip("\\").split("\\")
        share_root = f"\\\\{parts[0]}\\{parts[1]}"
        
        # Build net use command
        if domain and "\\" not in username:
            user_spec = f"{domain}\\{username}"
        else:
            user_spec = username
        
        cmd = [
            "net", "use", share_root, password,
            f"/user:{user_spec}",
            "/persistent:no"
        ]
        
        result = subprocess.run(cmd, capture_output=True, timeout=15, shell=False)
        
        if result.returncode == 0:
            return True, f"Successfully mounted {share_root}"
        
        stderr = result.stderr.decode("utf-8", errors="ignore")
        return False, f"Mount failed: {stderr[:100]}"
    
    except Exception as e:
        return False, f"Mount error: {str(e)[:100]}"


def disconnect_network_share(unc_path: str) -> tuple[bool, str]:
    """
    Disconnect network share on Windows.
    
    Returns:
        (success, message)
    """
    if platform.system() != "Windows":
        return True, "Not applicable on this platform"
    
    try:
        parts = unc_path.strip("\\").split("\\")
        share_root = f"\\\\{parts[0]}\\{parts[1]}"
        
        cmd = ["net", "use", share_root, "/delete", "/yes"]
        result = subprocess.run(cmd, capture_output=True, timeout=10, shell=False)
        
        if result.returncode == 0:
            return True, "Disconnected"
        
        # It's ok if already disconnected
        return True, "Already disconnected or not mounted"
    
    except Exception as e:
        return True, "Disconnection skipped"


def test_network_credentials(
    unc_path: str,
    username: str,
    password: str,
    domain: Optional[str] = None
) -> tuple[bool, str]:
    """
    Test network credentials by attempting to validate the UNC path.
    
    Returns:
        (credentials_valid, error_message)
    """
    # Validate UNC path format
    is_valid, error = validate_unc_path(unc_path)
    if not is_valid:
        return False, error
    
    # Test with provided credentials
    is_accessible, error = validate_unc_path(unc_path, username, password)
    
    if not is_accessible:
        return False, error
    
    return True, ""
