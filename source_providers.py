"""
source_providers.py
────────────────────
Pluggable source locations. The monitor service only ever talks to the
SourceProvider interface, so adding S3 / Azure Blob / Google Drive later
means writing one class — nothing in the queue, scheduler, or pipeline
needs to change.

    SourceProvider
        ├── LocalFolderProvider   (implemented)
        ├── NetworkLocationProvider (NEW - implemented)
        ├── FTPProvider           (implemented)
        ├── SFTPProvider          (extension point)
        └── CloudProvider         (extension point)

`list_pdfs()` returns logical handles; `fetch(handle)` returns a local
Path ready for the pipeline. For local sources fetch() is a no-op.
"""

from __future__ import annotations

import ftplib
from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass

from config import MonitorConfig, BASE_DIR

STAGING_DIR = BASE_DIR / "temp" / "staging"
STAGING_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class RemoteFile:
    """A discovered file plus enough metadata to dedup it."""
    name:     str            # display name
    ref:      str            # provider-specific reference (path or remote name)
    size:     int = 0
    modified: str = ""

    @property
    def uid(self) -> str:
        """Stable identity used for dedup across scans."""
        return f"{self.ref}|{self.size}"


class SourceProvider(ABC):
    def __init__(self, cfg: MonitorConfig):
        self.cfg = cfg

    @abstractmethod
    def list_pdfs(self) -> list[RemoteFile]:
        """Return all PDF files currently visible at the source."""

    @abstractmethod
    def fetch(self, remote: RemoteFile) -> Path:
        """Make the file available locally and return its Path."""

    def close(self) -> None:        # optional cleanup hook
        pass


# ── Local folder / network drive (UNC paths work here too) ─────────────

class LocalFolderProvider(SourceProvider):
    def list_pdfs(self) -> list[RemoteFile]:
        root = Path(self.cfg.source_path)
        if not root.exists():
            return []
        globber = root.rglob if self.cfg.recursive else root.glob
        out: list[RemoteFile] = []
        for p in globber("*"):
            if p.is_file() and p.suffix.lower() == ".pdf":
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                out.append(RemoteFile(name=p.name, ref=str(p.resolve()), size=size))
        return out

    def fetch(self, remote: RemoteFile) -> Path:
        return Path(remote.ref)


# ── Network Location (UNC paths with authentication) ─────────────────────

class NetworkLocationProvider(SourceProvider):
    """
    Provider for authenticated network shares (UNC paths).
    Handles Windows network shares with username/password authentication.
    
    Configuration:
        source_type: "network"
        source_path: UNC path (e.g. \\192.168.0.3\Team Share\Folder)
        network_username: username (or domain\username)
        network_domain: optional domain
        network_password: handled via network_auth module (never in config)
    """
    
    def __init__(self, cfg: MonitorConfig):
        super().__init__(cfg)
        self._mounted = False
        self._mount_network_share()
    
    def _mount_network_share(self) -> None:
        """Mount the network share with stored credentials."""
        from network_auth import get_network_credentials, mount_network_share
        
        try:
            # Get stored credentials
            creds = get_network_credentials(self.cfg.network_username)
            if not creds:
                raise RuntimeError(
                    f"No credentials found for {self.cfg.network_username}. "
                    f"Please authenticate via Configuration panel."
                )
            
            password, domain = creds
            
            # Mount the share
            success, message = mount_network_share(
                self.cfg.source_path,
                self.cfg.network_username,
                password,
                domain or self.cfg.network_domain
            )
            
            if success:
                self._mounted = True
            else:
                raise RuntimeError(f"Failed to mount network share: {message}")
        
        except Exception as e:
            raise RuntimeError(f"Network mount error: {str(e)}")
    
    def list_pdfs(self) -> list[RemoteFile]:
        """List PDFs from the network share."""
        try:
            root = Path(self.cfg.source_path)
            if not root.exists():
                return []
            
            globber = root.rglob if self.cfg.recursive else root.glob
            out: list[RemoteFile] = []
            
            for p in globber("*"):
                if p.is_file() and p.suffix.lower() == ".pdf":
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    out.append(RemoteFile(
                        name=p.name,
                        ref=str(p.resolve()),
                        size=size
                    ))
            
            return out
        
        except Exception as e:
            raise RuntimeError(f"Network list error: {str(e)}")
    
    def fetch(self, remote: RemoteFile) -> Path:
        """Return the path from the network share (already mounted)."""
        return Path(remote.ref)
    
    def close(self) -> None:
        """Disconnect the network share."""
        if self._mounted:
            try:
                from network_auth import disconnect_network_share
                disconnect_network_share(self.cfg.source_path)
                self._mounted = False
            except Exception:
                pass


# ── FTP ────────────────────────────────────────────────────────────────

class FTPProvider(SourceProvider):
    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP()
        ftp.connect(self.cfg.remote_host, self.cfg.remote_port, timeout=30)
        ftp.login(self.cfg.remote_user, self.cfg.remote_pass)
        ftp.set_pasv(True)
        return ftp

    def list_pdfs(self) -> list[RemoteFile]:
        ftp = self._connect()
        try:
            ftp.cwd(self.cfg.remote_dir or "/")
            out: list[RemoteFile] = []
            try:
                for name, facts in ftp.mlsd():
                    if facts.get("type") == "file" and name.lower().endswith(".pdf"):
                        out.append(RemoteFile(
                            name=name, ref=name,
                            size=int(facts.get("size", 0)),
                            modified=facts.get("modify", ""),
                        ))
            except ftplib.error_perm:
                for name in ftp.nlst():
                    if name.lower().endswith(".pdf"):
                        out.append(RemoteFile(name=name, ref=name))
            return out
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def fetch(self, remote: RemoteFile) -> Path:
        local = STAGING_DIR / remote.name
        ftp = self._connect()
        try:
            ftp.cwd(self.cfg.remote_dir or "/")
            with open(local, "wb") as f:
                ftp.retrbinary(f"RETR {remote.ref}", f.write)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        return local


# ── Extension points (raise clearly until implemented) ─────────────────

class SFTPProvider(SourceProvider):
    def list_pdfs(self) -> list[RemoteFile]:
        raise NotImplementedError(
            "SFTP source not yet implemented. Install 'paramiko' and fill in "
            "list_pdfs()/fetch() following FTPProvider as a template."
        )

    def fetch(self, remote: RemoteFile) -> Path:
        raise NotImplementedError("SFTP fetch not implemented.")


class CloudProvider(SourceProvider):
    """Placeholder for S3 / Azure Blob / GCS / Google Drive."""
    def list_pdfs(self) -> list[RemoteFile]:
        raise NotImplementedError(
            "Cloud source not yet implemented. Add the SDK (boto3 / "
            "azure-storage-blob / google-cloud-storage) and implement "
            "list_pdfs()/fetch() — download objects into STAGING_DIR."
        )

    def fetch(self, remote: RemoteFile) -> Path:
        raise NotImplementedError("Cloud fetch not implemented.")


_PROVIDERS = {
    "local":   LocalFolderProvider,
    "network": NetworkLocationProvider,
    "ftp":     FTPProvider,
    "sftp":    SFTPProvider,
    "cloud":   CloudProvider,
}


def get_provider(cfg: MonitorConfig) -> SourceProvider:
    cls = _PROVIDERS.get(cfg.source_type.lower())
    if cls is None:
        raise ValueError(
            f"Unknown source_type '{cfg.source_type}'. "
            f"Valid options: {', '.join(_PROVIDERS)}"
        )
    return cls(cfg)
