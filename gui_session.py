"""Build desktop backup sessions without depending on Qt."""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import backup
from credentials import CredentialSession, SOCKET_ENVIRONMENT_VARIABLE


@dataclass(frozen=True)
class BackupSettings:
    """Public connection and mirror options; credentials are kept separately."""

    host: str
    destination: Path
    home_only: bool = False
    prepare_nextcloud: bool = False
    remote_sudo: bool = True


class GuiSession:
    """Keep authentication in memory and share one OpenSSH connection."""

    def __init__(self, settings: BackupSettings, passwords: dict[str, str]) -> None:
        self.settings = settings
        self.credentials = CredentialSession(passwords)

    def environment(self) -> dict[str, str]:
        """Expose helper paths and a private socket address, never passwords."""
        environment = dict(os.environ)
        environment[SOCKET_ENVIRONMENT_VARIABLE] = str(self.credentials.socket_path)
        environment["SSH_ASKPASS"] = str(backup.PROJECT_DIRECTORY / "lib" / "ssh-askpass.sh")
        environment["SSH_ASKPASS_REQUIRE"] = "force"
        environment["SUDO_ASKPASS"] = str(backup.PROJECT_DIRECTORY / "lib" / "sudo-askpass.sh")
        return environment

    def master_command(self) -> list[str]:
        """Authenticate once; unknown or changed host keys must be verified first."""
        return [
            "ssh", "-M", "-N", "-f", "-S", str(self.credentials.control_path),
            "-o", "ControlPersist=60", "-o", "BatchMode=no",
            "-o", "NumberOfPasswordPrompts=1", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "--", self.settings.host,
        ]

    def backup_command(self, dry_run: bool) -> list[str]:
        """Elevate the receiver while SSH still runs as the desktop user."""
        command = [
            "sudo", "-A", f"--preserve-env=SSH_AUTH_SOCK,{SOCKET_ENVIRONMENT_VARIABLE}",
            "--", sys.executable, str(backup.PROJECT_DIRECTORY / "backup.py"), "mirror",
            self.settings.host, str(self.settings.destination),
            "--ssh-control-path", str(self.credentials.control_path), "--progress",
        ]
        if self.settings.remote_sudo:
            command.append("--remote-sudo-password")
        else:
            command.append("--no-remote-sudo")
        if self.settings.home_only:
            command.append("--home-only")
        elif self.settings.prepare_nextcloud:
            command.append("--prepare-nextcloud")
        if dry_run:
            command.append("--dry-run")
        return command

    def disconnect(self) -> None:
        """Close this operation's master connection, including active transfers."""
        command = [
            "ssh", "-S", str(self.credentials.control_path), "-O", "exit",
            "-o", "BatchMode=yes", "--", self.settings.host,
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)

    def close(self) -> None:
        """Close the connection and remove the local credential socket."""
        try:
            self.disconnect()
        finally:
            self.credentials.close()
