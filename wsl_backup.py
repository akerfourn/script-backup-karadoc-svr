"""Mount the selected ext4 volume and run LiteCloud for the Windows launcher."""

import argparse
import fcntl
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import backup


MOUNT_POINT = Path("/media/backup")
USB_READY = "[wsl-backup:ready]"
USB_RELEASED = "[wsl-backup:released]"


def read_passwords() -> dict[str, str]:
    """Read one bounded message from the Windows process pipe."""
    try:
        passwords = json.loads(sys.stdin.buffer.readline(65536))
    except (UnicodeError, ValueError) as error:
        raise ValueError("Message d’authentification Windows invalide.") from error
    if not isinstance(passwords, dict) or set(passwords) != {"ssh", "sudo-remote"}:
        raise ValueError("Message d’authentification Windows invalide.")
    if any(not isinstance(value, str) for value in passwords.values()):
        raise ValueError("Message d’authentification Windows invalide.")
    return passwords


def wait_for_partition(expected_uuid: str) -> Path:
    """Wait for USB storage enumeration without assuming a Linux device name."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        devices = subprocess.check_output(
            ["lsblk", "--json", "--list", "--output", "PATH,UUID,FSTYPE,TRAN,PKNAME", "--paths"],
            text=True, timeout=10,
        )
        block_devices = json.loads(devices)["blockdevices"]
        matches = [
            device for device in block_devices
            if (device.get("uuid") or "").lower() == expected_uuid
        ]
        if len(matches) > 1:
            raise ValueError("Plusieurs volumes ont l’UUID ext4 attendu.")
        if len(matches) == 1:
            device = matches[0]
            parent = device["pkname"]
            parent_is_usb = any(
                entry["path"] == parent and entry["tran"] == "usb"
                for entry in block_devices
            )
            if device["fstype"] != "ext4" or not (device["tran"] == "usb" or parent_is_usb):
                raise ValueError("Le volume attendu doit être un volume ext4 sur USB.")
            return Path(device["path"])
        time.sleep(2)
    raise ValueError("Partition ext4 introuvable après 90 secondes. Vérifier le disque et son UUID.")


def mounted_targets(device: Path) -> set[str]:
    """Find mounts of this block device across visible Linux mount namespaces."""
    info = device.stat()
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError("La partition détectée n’est plus un périphérique bloc.")
    identity = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    targets = set()
    for mount_info in Path("/proc").glob("[0-9]*/mountinfo"):
        try:
            for line in mount_info.read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split()
                if fields[2] == identity:
                    targets.add(fields[4])
        except (FileNotFoundError, ProcessLookupError):
            continue
    return targets


@contextmanager
def mounted_backup(device: Path, dry_run: bool, session: str) -> Iterator[Path]:
    """Reuse an existing matching mount, or own the temporary mount lifecycle."""
    if MOUNT_POINT.is_symlink():
        raise ValueError(f"Le point de montage {MOUNT_POINT} est un lien symbolique.")
    targets = mounted_targets(device)
    if os.path.ismount(MOUNT_POINT):
        mounted_device = MOUNT_POINT.stat().st_dev
        if mounted_device != device.stat().st_rdev:
            raise ValueError(f"Un autre volume est déjà monté sur {MOUNT_POINT}.")
        backup.validate_destination(MOUNT_POINT, [], require_writable=not dry_run)
        print(f"Montage existant réutilisé : {MOUNT_POINT}", flush=True)
        yield MOUNT_POINT
        return
    if targets:
        raise ValueError("La partition est déjà montée ailleurs dans WSL.")
    MOUNT_POINT.mkdir(parents=True, exist_ok=True)
    if any(MOUNT_POINT.iterdir()):
        raise ValueError(f"Le dossier {MOUNT_POINT} doit être vide avant le montage.")
    mode = "ro,noload" if dry_run else "rw"
    print(f"Montage ext4 ({mode}) sur {MOUNT_POINT}…", flush=True)
    subprocess.run([
        "mount", "-t", "ext4", "-o", f"{mode},nodev,nosuid,noexec",
        "--", str(device), str(MOUNT_POINT),
    ], check=True)
    try:
        yield MOUNT_POINT
    finally:
        print("Démontage du disque…", flush=True)
        subprocess.run(["umount", "--", str(MOUNT_POINT)], check=True)
        if mounted_targets(device):
            raise ValueError("Un montage du disque est encore actif dans WSL.")
        print(USB_RELEASED + session, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=backup.remote_host)
    parser.add_argument("ext4_uuid", type=uuid.UUID)
    parser.add_argument("ssh_user")
    parser.add_argument("session")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Le lanceur WSL doit être exécuté en tant que root.")
        passwords = read_passwords()
        with Path("/run/lock/litecloud-backup-wsl.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Une sauvegarde Windows est déjà active dans WSL.") from error
            print(USB_READY + options.session, flush=True)
            print("Attente du volume ext4 dans WSL…", flush=True)
            device = wait_for_partition(str(options.ext4_uuid))
            with mounted_backup(device, options.dry_run, options.session) as destination:
                arguments = [
                    "backup-litecloud", options.host, str(destination),
                    "--ssh-as", options.ssh_user, "--interactive-ssh", "--progress",
                ]
                if options.dry_run:
                    arguments.append("--dry-run")
                settings = backup.parse_arguments(arguments)
                print(f"Connexion à {options.host}…", flush=True)
                with backup.interactive_sudo_session(settings, passwords) as authenticated:
                    backup.backup_litecloud(authenticated)
        print("Terminé.", flush=True)
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"Erreur : {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("Sauvegarde interrompue.", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
