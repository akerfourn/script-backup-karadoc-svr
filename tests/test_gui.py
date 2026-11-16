"""Check the Qt form and process lifecycle without connecting to a server."""

import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from gui_session import BackupSettings


@unittest.skipUnless(importlib.util.find_spec("PySide6") is not None, "PySide6 facultatif absent")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        environment = patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"})
        environment.start()
        cls.addClassCleanup(environment.stop)
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance()
        if cls.application is None:
            cls.application = QApplication([])

    def setUp(self) -> None:
        from backup_gui import BackupWindow
        self.window = BackupWindow()
        self.addCleanup(self.window.close)

    def test_form_should_enable_a_separate_remote_password_when_selected(self) -> None:
        form = self.window.form
        self.assertFalse(form.sudo_password.isEnabled())

        form.same_password.setChecked(False)

        self.assertTrue(form.sudo_password.isEnabled())
        form.remote_sudo.setChecked(False)
        self.assertFalse(form.sudo_password.isEnabled())

    def test_window_should_connect_before_starting_the_mirror_and_retain_passwords_in_memory(self) -> None:
        from PySide6.QtCore import QProcess
        settings = BackupSettings("server", Path("/mnt/backup"))
        self.window.form.ssh_password.setText("test-password")
        with patch.object(self.window, "collect_settings", return_value=settings):
            with patch.object(self.window, "local_password", return_value="local-password"):
                with patch("backup_gui.GuiSession") as session_class:
                    session = session_class.return_value
                    session.master_command.return_value = ["ssh", "master"]
                    session.backup_command.return_value = ["sudo", "mirror"]
                    session.environment.return_value = {}
                    with patch.object(self.window, "start_command") as start:
                        self.window.simulate()
                        start.assert_called_once_with(["ssh", "master"])
                        self.assertFalse(self.window.form.synchronize.isEnabled())
                        self.window.process_finished(0, QProcess.ExitStatus.NormalExit)
                        start.assert_called_with(["sudo", "mirror"])
                        session.backup_command.assert_called_once_with(True)
                        self.window.process_finished(0, QProcess.ExitStatus.NormalExit)
                    session.close.assert_called_once()
        self.assertIsNone(self.window.session)
        self.assertTrue(self.window.form.synchronize.isEnabled())
        self.assertEqual(self.window.form.ssh_password.text(), "test-password")

    def test_window_should_show_real_local_process_output_in_its_journal(self) -> None:
        self.window.start_command([sys.executable, "-c", "print('journal de test')"])

        self.assertTrue(self.window.process.waitForFinished(5000))

        self.assertIn("journal de test", self.window.form.journal.toPlainText())
        self.assertEqual(self.window.form.status.text(), "Terminé.")

    def test_window_should_report_a_disk_error_even_when_the_message_arrives_in_chunks(self) -> None:
        from PySide6.QtCore import QProcess
        with patch.object(self.window.process, "isOpen", return_value=True):
            with patch.object(self.window.process, "readAllStandardOutput", side_effect=[
                b"rsync: [generator] flush of delete-delay buffer: Input/out",
                b"put error (5)\n",
            ]):
                self.window.read_output()
                self.window.read_output()
        self.assertTrue(self.window.disk_error)
        self.assertIn("Erreur d'accès au disque", self.window.form.status.text())

        self.window.process_finished(23, QProcess.ExitStatus.NormalExit)

        self.assertIn("Échec : erreur d'accès au disque", self.window.form.status.text())
        self.assertTrue(self.window.form.simulate.isEnabled())

    def test_stop_should_wait_for_the_receiver_after_disconnecting_ssh(self) -> None:
        session = Mock()
        self.window.session = session
        with patch.object(self.window.process, "terminate") as terminate:
            self.window.stop()
        session.credentials.cancel.assert_called_once()
        session.disconnect.assert_called_once()
        terminate.assert_not_called()
        self.assertTrue(self.window.cancelled)
        self.assertFalse(self.window.form.stop.isEnabled())
        self.assertIs(self.window.session, session)
        self.window.finish_operation("Interrompu.")

    def test_stop_should_terminate_an_unfinished_ssh_login(self) -> None:
        self.window.session = Mock()
        self.window.connecting = True
        with patch.object(self.window.process, "terminate") as terminate:
            self.window.stop()
        terminate.assert_called_once()
        self.window.finish_operation("Interrompu.")

    def test_close_should_clear_password_fields(self) -> None:
        self.window.form.ssh_password.setText("ssh-test-secret")
        self.window.form.sudo_password.setText("sudo-test-secret")

        self.window.close()

        self.assertEqual(self.window.form.ssh_password.text(), "")
        self.assertEqual(self.window.form.sudo_password.text(), "")

    def test_close_should_wait_for_the_active_operation_to_stop(self) -> None:
        from PySide6.QtCore import QProcess
        session = Mock()
        self.window.session = session
        self.window.form.ssh_password.setText("test-secret")
        with patch.object(self.window.process, "terminate"):
            self.window.close()
        self.assertTrue(self.window.close_requested)
        session.disconnect.assert_called_once()

        self.window.process_finished(1, QProcess.ExitStatus.NormalExit)

        session.close.assert_called_once()
        self.assertIsNone(self.window.session)
        self.assertEqual(self.window.form.ssh_password.text(), "")
