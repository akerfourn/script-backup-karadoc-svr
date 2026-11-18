"""Check memory-only authentication and sudo/protocol separation locally."""

import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

import backup
from credentials import CredentialSession, request_secret, SOCKET_ENVIRONMENT_VARIABLE
from gui_session import BackupSettings, GuiSession
from ssh_transport import SudoHandshake


class CredentialTests(unittest.TestCase):
    def test_session_should_deliver_credentials_through_a_private_socket(self) -> None:
        with contextlib.closing(CredentialSession({"ssh": "test-password"})) as session:
            with patch.dict(os.environ, {SOCKET_ENVIRONMENT_VARIABLE: str(session.socket_path)}):
                self.assertEqual(request_secret("ssh"), "test-password")
                self.assertEqual(request_secret("unknown"), "")
            self.assertEqual(stat.S_IMODE(session.socket_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(session.socket_path.parent.stat().st_mode), 0o700)
            directory = session.socket_path.parent
        self.assertFalse(directory.exists())
        self.assertEqual(session.server.secrets, {})

    def test_session_should_refuse_newlines_that_could_corrupt_remote_stdin(self) -> None:
        with self.assertRaisesRegex(ValueError, "saut de ligne"):
            CredentialSession({"sudo-remote": "password\ncommand"})

    def test_session_should_propagate_cancellation_to_the_receiver(self) -> None:
        with contextlib.closing(CredentialSession({})) as session:
            with patch.dict(os.environ, {SOCKET_ENVIRONMENT_VARIABLE: str(session.socket_path)}):
                backup.check_cancelled()

                session.cancel()

                with self.assertRaises(KeyboardInterrupt):
                    backup.check_cancelled()

    def test_session_should_not_retry_the_same_local_password_but_allow_remote_channels(self) -> None:
        with contextlib.closing(CredentialSession({"sudo-local": "local", "sudo-remote": "remote"})) as session:
            with patch.dict(os.environ, {SOCKET_ENVIRONMENT_VARIABLE: str(session.socket_path)}):
                self.assertEqual(request_secret("sudo-local"), "local")
                self.assertEqual(request_secret("sudo-local"), "")
                self.assertEqual(request_secret("sudo-remote"), "remote")
                self.assertEqual(request_secret("sudo-remote"), "remote")

    def test_askpass_should_supply_only_the_requested_credential(self) -> None:
        with contextlib.closing(CredentialSession({"ssh": "ssh-test", "sudo-local": "local-test"})) as session:
            environment = dict(os.environ, **{SOCKET_ENVIRONMENT_VARIABLE: str(session.socket_path)})
            result = subprocess.run(
                [sys.executable, str(backup.PROJECT_DIRECTORY / "lib" / "askpass.py"), "ssh"],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            )
        self.assertEqual(result.stdout, b"ssh-test\n")
        self.assertEqual(result.stderr, b"")


class SudoHandshakeTests(unittest.TestCase):
    def test_handshake_should_handle_split_markers_without_leaking_into_protocol(self) -> None:
        handshake = SudoHandshake(b"PROMPT_TOKEN", b"READY_TOKEN")
        remote_input = io.BytesIO()
        output = b""
        with patch("ssh_transport.request_secret", return_value="sudo-test") as request:
            for value in b"before\nPROMPT_TOKENafter prompt\nREADY_TOKENafter ready\n":
                output += handshake.feed(bytes([value]), remote_input)
        self.assertTrue(handshake.is_ready)
        self.assertEqual(remote_input.getvalue(), b"sudo-test\n")
        self.assertEqual(output, b"before\nafter prompt\nafter ready\n")
        request.assert_called_once_with("sudo-remote")

    def test_handshake_should_not_send_a_password_when_sudo_needs_none(self) -> None:
        handshake = SudoHandshake(b"PROMPT_TOKEN", b"READY_TOKEN")
        remote_input = io.BytesIO()
        with patch("ssh_transport.request_secret") as request:
            output = handshake.feed(b"READY_TOKEN", remote_input)
        self.assertTrue(handshake.is_ready)
        self.assertEqual(output, b"")
        self.assertEqual(remote_input.getvalue(), b"")
        request.assert_not_called()

    def test_handshake_should_stop_after_one_rejected_password(self) -> None:
        handshake = SudoHandshake(b"PROMPT_TOKEN", b"READY_TOKEN")
        remote_input = io.BytesIO()
        with patch("ssh_transport.request_secret", return_value="sudo-test") as request:
            handshake.feed(b"PROMPT_TOKEN", remote_input)
            with self.assertRaisesRegex(ValueError, "refusé"):
                handshake.feed(b"PROMPT_TOKEN", remote_input)
        request.assert_called_once()
        self.assertEqual(remote_input.getvalue(), b"sudo-test\n")


class GuiSessionTests(unittest.TestCase):
    def test_session_should_keep_passwords_out_of_commands_and_environment(self) -> None:
        passwords = {"ssh": "ssh-test-secret", "sudo-local": "local-test-secret",
                     "sudo-remote": "remote-test-secret"}
        session = GuiSession(BackupSettings("server", Path("/mnt/backup")), passwords)
        with patch("gui_session.subprocess.run"):
            with contextlib.closing(session):
                exposed = repr(session.master_command()) + repr(session.backup_command(True))
                exposed += repr(session.environment())
                for secret in passwords.values():
                    self.assertNotIn(secret, exposed)
                self.assertIn("--dry-run", session.backup_command(True))
                self.assertIn("--remote-sudo-password", session.backup_command(True))


class SshErrorTests(unittest.TestCase):
    def test_supplied_passwords_should_reach_ssh_and_sudo_through_the_private_socket(self) -> None:
        options = backup.parse_arguments([
            "backup-litecloud", "server", "/media/backup", "--interactive-ssh",
        ])
        run = subprocess.run
        passwords = {"ssh": "test-ssh", "sudo-remote": "test-sudo"}

        def fake_ssh(command: list[str], **_kwargs: object) -> None:
            for secret in passwords.values():
                self.assertNotIn(secret, repr(command))
                self.assertNotIn(secret, repr(dict(os.environ)))
            if "-M" in command:
                helper = next(arg.split("=", 1)[1] for arg in command
                              if arg.startswith("SSH_ASKPASS="))
                result = run([helper], capture_output=True, check=True, text=True)
                self.assertEqual(result.stdout, "test-ssh\n")
                self.assertEqual(request_secret("sudo-remote"), "test-sudo")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("backup.subprocess.run", side_effect=fake_ssh) as ssh,
            backup.interactive_sudo_session(options, passwords) as session,
        ):
            self.assertTrue(session.remote_sudo_password)
        self.assertEqual(ssh.call_count, 2)

    def test_cli_should_explain_batch_authentication_when_ssh_fails(self) -> None:
        output = io.StringIO()
        with patch("backup.shutil.which", return_value="/usr/bin/tool"):
            with patch("backup.prepare_nextcloud", side_effect=subprocess.CalledProcessError(255, "ssh")):
                with contextlib.redirect_stderr(output):
                    result = backup.main(["prepare-nextcloud", "server"])
        self.assertEqual(result, 1)
        self.assertIn("ssh server", output.getvalue())
        self.assertIn("BatchMode=yes", output.getvalue())
        self.assertNotIn("Command '[", output.getvalue())

    def test_ssh_should_allow_prompts_when_interactive_mode_is_selected(self) -> None:
        options = backup.parse_arguments(["prepare-nextcloud", "server", "--interactive-ssh"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("BatchMode=no", backup.ssh_command(options))
