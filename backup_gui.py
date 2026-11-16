#!/usr/bin/env python3
"""Run the rsync mirror from a small PySide6 desktop window."""

import argparse
import codecs
import os
from pathlib import Path
import re
import subprocess
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMainWindow, QMessageBox

import backup
from gui_form import BackupForm
from gui_session import BackupSettings, GuiSession


class BackupWindow(QMainWindow):
    """Manage one SSH session and one mirror operation at a time."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sauvegarde distante · rsync")
        self.resize(880, 650)
        self.form = BackupForm()
        self.setCentralWidget(self.form)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.form.simulate.clicked.connect(self.simulate)
        self.form.synchronize.clicked.connect(self.synchronize)
        self.form.stop.clicked.connect(self.stop)
        self.session: GuiSession | None = None
        self.connecting = False
        self.dry_run = True
        self.cancelled = False
        self.close_requested = False
        self.disk_error = False
        self.output_tail = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def simulate(self) -> None:
        """Inspect changes without modifying the destination or the exports."""
        self.start_operation(dry_run=True)

    def synchronize(self) -> None:
        """Apply the explicitly requested mirror operation."""
        self.start_operation(dry_run=False)

    def collect_settings(self) -> BackupSettings:
        """Validate the host and disk before any authentication attempt."""
        host = backup.remote_host(self.form.host.text().strip())
        names = ["home"]
        if not self.form.home_only.isChecked():
            names.extend(["nextcloud", "config"])
        destination = backup.validate_destination(
            Path(self.form.destination.text()), names, allow_missing=True
        )
        return BackupSettings(host, destination, self.form.home_only.isChecked(),
                              self.form.prepare.isChecked(), self.form.remote_sudo.isChecked())

    def local_password(self) -> str | None:
        """Request local administrator access once if sudo has no valid ticket."""
        result = subprocess.run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            return ""
        password, accepted = QInputDialog.getText(
            self, "Autorisation locale", "Mot de passe administrateur de ce PC :",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return None
        return password

    def start_operation(self, dry_run: bool) -> None:
        """Keep passwords in memory and authenticate the master SSH connection."""
        if self.session is not None:
            return
        try:
            settings = self.collect_settings()
            local_password = self.local_password()
            if local_password is None:
                return
            sudo_password = self.form.sudo_password.text()
            if self.form.same_password.isChecked():
                sudo_password = self.form.ssh_password.text()
            passwords = {"ssh": self.form.ssh_password.text(), "sudo-remote": sudo_password,
                         "sudo-local": local_password}
            self.session = GuiSession(settings, passwords)
        except (OSError, ValueError, argparse.ArgumentTypeError) as error:
            QMessageBox.warning(self, "Sauvegarde impossible", str(error))
            return
        self.dry_run = dry_run
        self.cancelled = False
        self.connecting = True
        self.disk_error = False
        self.output_tail = ""
        self.form.set_busy(True)
        self.form.journal.clear()
        self.decoder.reset()
        self.form.status.setText("Connexion SSH…")
        environment = QProcessEnvironment()
        for name, value in self.session.environment().items():
            environment.insert(name, value)
        self.process.setProcessEnvironment(environment)
        self.start_command(self.session.master_command())

    def start_command(self, command: list[str]) -> None:
        """Run external tools without a shell or credentials in their arguments."""
        self.process.start(command[0], command[1:])

    def read_output(self) -> None:
        """Decode progress output and show a bounded live journal."""
        if not self.process.isOpen():
            return
        data = bytes(self.process.readAllStandardOutput())
        output = self.decoder.decode(data).replace("\r", "\n")
        diagnostic = self.output_tail + output
        self.output_tail = diagnostic[-512:]
        if re.search(r"(?:^|\n)rsync: [^\n]*\((?:5|30)\)", diagnostic) is not None:
            self.disk_error = True
            self.form.status.setText("Erreur d'accès au disque — arrêt conseillé. Rsync est encore actif.")
        if output != "":
            cursor = self.form.journal.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(output)
            self.form.journal.setTextCursor(cursor)
            self.form.journal.ensureCursorVisible()

    def process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        """Start the mirror only after SSH authentication succeeds."""
        self.read_output()
        if self.connecting and exit_code == 0 and not self.cancelled and self.session is not None:
            self.connecting = False
            self.form.status.setText("Simulation : analyse des fichiers…" if self.dry_run
                                     else "Synchronisation en cours…")
            self.start_command(self.session.backup_command(self.dry_run))
            return
        message = "Terminé."
        if self.disk_error:
            message = "Échec : erreur d'accès au disque. Consulter le journal."
        elif self.cancelled:
            message = "Interrompu. La même opération peut être relancée."
        elif exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            message = "Échec : consulter le journal."
        self.finish_operation(message)

    def process_error(self, error: QProcess.ProcessError) -> None:
        """Handle startup failures for which Qt does not emit finished."""
        if error == QProcess.ProcessError.FailedToStart:
            self.finish_operation(f"Impossible de lancer le processus : {self.process.errorString()}")

    def finish_operation(self, message: str) -> None:
        """Close the operation only after the receiver has finished exiting."""
        if self.session is not None:
            try:
                self.session.close()
            except (OSError, subprocess.TimeoutExpired) as error:
                self.form.journal.appendPlainText(f"Fermeture SSH : {error}")
            self.session = None
        self.form.status.setText(message)
        self.form.set_busy(False)
        if self.close_requested:
            self.close()

    def stop(self) -> None:
        """Disconnect SSH, then let the receiver finish saving partial files."""
        self.cancelled = True
        self.form.stop.setEnabled(False)
        self.form.status.setText("Arrêt en cours…")
        if self.session is not None:
            self.session.credentials.cancel()
            try:
                self.session.disconnect()
            except (OSError, subprocess.TimeoutExpired) as error:
                self.form.journal.appendPlainText(f"Arrêt SSH : {error}")
        if self.connecting:
            self.process.terminate()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Allow an active operation to finish stopping before the window closes."""
        if self.session is not None:
            self.close_requested = True
            self.stop()
            event.ignore()
            return
        self.form.ssh_password.clear()
        self.form.sudo_password.clear()
        event.accept()


def main() -> int:
    """Launch as the desktop user; only the receiving CLI is elevated."""
    application = QApplication(sys.argv)
    if os.geteuid() == 0:
        QMessageBox.critical(None, "Lancement", "Lancer l'interface sans sudo.")
        return 1
    window = BackupWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
