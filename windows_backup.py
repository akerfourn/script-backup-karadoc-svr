"""Native Windows launcher for the LiteCloud backup in Ubuntu under WSL."""

import argparse
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
import uuid
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk


PROJECT = Path(__file__).resolve().parent
USB_READY = "[wsl-backup:ready]"
USB_RELEASED = "[wsl-backup:released]"


def windows_command(command: list[str], timeout: int = 30) -> str:
    """Run a Windows or WSL command without opening a console."""
    result = subprocess.run(
        command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        env={**os.environ, "WSL_UTF8": "1"}, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(diagnostic or f"{command[0]} a échoué (code {result.returncode}).")
    return result.stdout


def usbipd_path() -> str:
    """Find the installed USB/IP Windows client."""
    installed = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "usbipd-win/usbipd.exe"
    found = str(installed) if installed.is_file() else shutil.which("usbipd.exe")
    if found is None:
        raise RuntimeError("Installer usbipd-win pour présenter un disque USB à WSL.")
    return found


def usb_devices(usbipd: str) -> list[dict]:
    """Return current devices with valid USB bus IDs from usbipd."""
    state = json.loads(windows_command([usbipd, "state"]))
    return [
        device for device in state["Devices"]
        if re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", device.get("BusId") or "")
    ]


def share_device(usbipd: str, bus: str) -> None:
    """Request UAC only for the initial persistent sharing of this device."""
    if re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", bus) is None:
        raise ValueError("Identifiant de bus USB invalide.")
    executable = usbipd.replace("'", "''")
    script = (
        f"$process = Start-Process -FilePath '{executable}' "
        f"-ArgumentList @('bind','--busid','{bus}') -Verb RunAs -WindowStyle Hidden -Wait -PassThru\n"
        "if ($null -eq $process) { exit 1 }; exit $process.ExitCode"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    windows_command(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], timeout=120)


def run_backup(
    host: str, distribution: str, volume_uuid: str, bus: str, dry_run: bool,
    passwords: dict[str, str], events: queue.Queue,
) -> None:
    """Start the Linux worker, attach USB when ready and follow the live log."""
    def log(message: str) -> None:
        events.put(("log", message))

    usbipd = usbipd_path()
    chosen = next((device for device in usb_devices(usbipd) if device["BusId"] == bus), None)
    if chosen is None:
        raise RuntimeError("Le disque USB sélectionné a disparu. Actualiser la liste.")
    if not chosen.get("PersistedGuid"):
        log("Autorisation Windows requise pour le premier partage USB…")
        share_device(usbipd, bus)

    wsl = ["wsl.exe", "--distribution", distribution]
    log(f"Démarrage de {distribution}…")
    linux_user = windows_command([*wsl, "--exec", "id", "-un"]).strip()
    if not linux_user:
        raise RuntimeError("Utilisateur Ubuntu par défaut introuvable.")
    helper = windows_command(
        [*wsl, "--exec", "wslpath", "-a", "-u", str(PROJECT / "wsl_backup.py")]
    ).strip()
    session = uuid.uuid4().hex
    command = [
        *wsl, "--user", "root", "--exec", "python3", "-u", helper, host, volume_uuid,
        linux_user, session,
    ]
    if dry_run:
        command.append("--dry-run")

    attached_here = False
    released = False
    attach_error = None
    payload = json.dumps(passwords, ensure_ascii=True) + "\n"
    passwords.clear()
    if len(payload) >= 65536:
        raise ValueError("Le message d’authentification est trop long.")
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", env={**os.environ, "WSL_UTF8": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        try:
            process.stdin.write(payload)
        finally:
            process.stdin.close()
            del payload
        for output in process.stdout:
            line = output.strip()
            if line == USB_READY + session:
                try:
                    current = next((
                        device for device in usb_devices(usbipd)
                        if device["BusId"] == bus and device["InstanceId"] == chosen["InstanceId"]
                    ), None)
                    if current is None:
                        raise RuntimeError("Le périphérique USB a changé avant le rattachement.")
                    if not current.get("ClientIPAddress"):
                        log(f"Rattachement USB {bus} à WSL…")
                        windows_command([usbipd, "attach", "--wsl", "--busid", bus])
                        attached_here = True
                    else:
                        log("Le disque est déjà rattaché à WSL.")
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    attach_error = error
                    log(f"Rattachement USB : {error}")
            elif line == USB_RELEASED + session:
                released = True
            elif line:
                log(line)
        code = process.wait()
    if attached_here and released:
        current = next((
            device for device in usb_devices(usbipd)
            if device["BusId"] == bus and device["InstanceId"] == chosen["InstanceId"]
        ), None)
        if current is not None and current.get("ClientIPAddress"):
            windows_command([usbipd, "detach", "--busid", bus])
        log("Disque démonté et détaché de WSL.")
    elif attached_here:
        log("Disque laissé rattaché. Vérifier son montage avant de le débrancher.")
    if attach_error is not None:
        raise RuntimeError(f"Impossible de rattacher le disque : {attach_error}")
    if code != 0:
        raise RuntimeError(f"La sauvegarde s’est terminée en erreur (code {code}).")


class BackupWindow:
    def __init__(self, root: tk.Tk, defaults: argparse.Namespace) -> None:
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.host = tk.StringVar(value=defaults.host)
        self.volume_uuid = tk.StringVar(value=defaults.uuid)
        self.distribution = tk.StringVar(value=defaults.distribution)
        self.device = tk.StringVar()
        self.ssh = tk.StringVar()
        self.sudo = tk.StringVar()
        self.same_password = tk.BooleanVar(value=True)
        self.dry_run = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Prêt")
        root.title("Sauvegarde LiteCloud")
        root.geometry("760x530")
        root.minsize(540, 420)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        root.after(100, self.consume_events)
        root.after(0, self.refresh)

    def build(self) -> None:
        panel = ttk.Frame(self.root, padding=16)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(9, weight=1)
        for row, (label, variable, secret) in enumerate((
            ("Serveur SSH", self.host, False), ("Distribution WSL", self.distribution, False),
            ("UUID ext4", self.volume_uuid, False), ("Mot de passe SSH", self.ssh, True),
            ("Mot de passe sudo distant", self.sudo, True),
        )):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(panel, textvariable=variable, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", padx=6)
            if variable is self.sudo:
                self.sudo_entry = entry
        ttk.Label(panel, text="Périphérique USB").grid(row=5, column=0, sticky="w")
        self.devices = ttk.Combobox(panel, textvariable=self.device, state="readonly")
        self.devices.grid(row=5, column=1, sticky="ew", padx=6)
        self.refresh_button = ttk.Button(panel, text="Actualiser", command=self.refresh)
        self.refresh_button.grid(row=5, column=2)
        self.same_checkbox = ttk.Checkbutton(
            panel, text="Même mot de passe pour SSH et sudo",
            variable=self.same_password, command=self.update_password_field,
        )
        self.same_checkbox.grid(row=6, column=1, sticky="w")
        self.update_password_field()
        ttk.Checkbutton(panel, text="Simulation", variable=self.dry_run).grid(
            row=7, column=1, sticky="w", pady=3,
        )
        self.start_button = ttk.Button(panel, text="Lancer", command=self.start)
        self.start_button.grid(row=7, column=2, sticky="ew")
        ttk.Label(panel, textvariable=self.status).grid(row=8, column=0, sticky="w")
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.grid(row=8, column=1, columnspan=2, sticky="ew", pady=8)
        self.log = tk.Text(panel, state="disabled", wrap="none", height=10)
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew")
        scrollbar = ttk.Scrollbar(panel, command=self.log.yview)
        scrollbar.grid(row=9, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def update_password_field(self) -> None:
        self.sudo_entry.configure(state="disabled" if self.same_password.get() else "normal")

    def start_worker(self, work: Callable[[], None]) -> None:
        self.busy = True
        self.start_button.state(["disabled"])
        self.refresh_button.state(["disabled"])
        self.progress.start(18)

        def worker() -> None:
            try:
                work()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("idle", None))

        threading.Thread(target=worker, daemon=True).start()

    def refresh(self) -> None:
        self.status.set("Recherche des disques USB…")

        def scan() -> None:
            devices = usb_devices(usbipd_path())
            storage = [
                f"{device['BusId']}  {device['Description']}" for device in devices
                if re.search(r"stockage|storage|SCSI|\bUAS\b|disk|disque",
                             device.get("Description", ""), re.IGNORECASE)
            ]
            self.events.put(("devices", storage))
            self.events.put(("status", "Prêt" if storage else "Aucun disque USB détecté"))

        self.start_worker(scan)

    def start(self) -> None:
        host = self.host.get().strip()
        distribution = self.distribution.get().strip()
        selection = self.device.get().split(maxsplit=1)
        try:
            volume_uuid = str(uuid.UUID(self.volume_uuid.get().strip()))
            if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", host) or host.startswith("-"):
                raise ValueError("Renseigner un alias ou utilisateur@serveur.")
            if not distribution or distribution.startswith("-") or not selection:
                raise ValueError("Choisir une distribution WSL et un disque USB.")
        except ValueError as error:
            messagebox.showwarning("Sauvegarde", str(error), parent=self.root)
            return
        ssh_password = self.ssh.get()
        passwords = {
            "ssh": ssh_password,
            "sudo-remote": ssh_password if self.same_password.get() else self.sudo.get(),
        }
        self.ssh.set("")
        self.sudo.set("")
        dry_run = self.dry_run.get()
        self.status.set("Démarrage…")

        def run() -> None:
            run_backup(host, distribution, volume_uuid, selection[0], dry_run, passwords, self.events)
            self.events.put(("status", "Simulation terminée" if dry_run else "Sauvegarde terminée"))

        self.start_worker(run)

    def consume_events(self) -> None:
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ("log", "error"):
                self.log.configure(state="normal")
                self.log.insert("end", value + "\n")
                if int(self.log.index("end-1c").split(".")[0]) > 3000:
                    self.log.delete("1.0", "501.0")
                self.log.see("end")
                self.log.configure(state="disabled")
                self.status.set("Échec. Voir le journal." if kind == "error" else value[:90])
            elif kind == "devices":
                previous = self.device.get()
                self.devices.configure(values=value)
                self.device.set(previous if previous in value else (value[0] if len(value) == 1 else ""))
            elif kind == "status":
                self.status.set(value)
            elif kind == "idle":
                self.progress.stop()
                self.busy = False
                self.start_button.state(["!disabled"])
                self.refresh_button.state(["!disabled"])
        self.root.after(100, self.consume_events)

    def close(self) -> None:
        if self.busy:
            messagebox.showinfo("Sauvegarde", "Attendre la fin de l’opération avant de fermer.")
            return
        self.root.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="")
    parser.add_argument("--uuid", default="")
    parser.add_argument("--distribution", default="Ubuntu")
    defaults = parser.parse_args()
    window = tk.Tk()
    BackupWindow(window, defaults)
    window.mainloop()
