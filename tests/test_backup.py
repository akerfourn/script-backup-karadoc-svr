"""Exercise real rsync transfers through a local SSH double, without networking."""

import argparse
import contextlib
import ctypes
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backup
from credentials import CredentialSession, SOCKET_ENVIRONMENT_VARIABLE


FAKE_SSH = """#!/usr/bin/env python3
import os
import shlex
import sys

arguments = sys.argv[1:]
while arguments[0] in ("-o", "-S", "-l"):
    arguments = arguments[2:]
if arguments[0] == "--":
    arguments = arguments[1:]
command = shlex.split(" ".join(arguments[1:]))
os.execvp(command[0], command)
"""

FAKE_SUDO = """#!/usr/bin/env python3
import os
import sys

arguments = sys.argv[1:]
while arguments[0].startswith("-"):
    if arguments[0] == "--":
        arguments = arguments[1:]
        break
    arguments = arguments[1:]
os.execvp(arguments[0], arguments)
"""

INTERRUPTING_SSH = """#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys

arguments = sys.argv[1:]
while arguments[0] in ('-o', '-S', '-l'):
    arguments = arguments[2:]
if arguments[0] == '--':
    arguments = arguments[1:]
command = shlex.split(' '.join(arguments[1:]))
if command[0] != 'rsync':
    os.execvp(command[0], command)
with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
    transferred = 0
    while True:
        data = os.read(process.stdout.fileno(), 32768)
        if data == b'':
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        transferred += len(data)
        if transferred >= 131072:
            process.terminate()
            process.communicate(timeout=5)
            sys.exit(12)
    sys.exit(process.wait())
"""

PASSWORD_SUDO = """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
ticket = Path(os.environ['BACKUP_TEST_SUDO_TICKET'])
if '-v' in arguments:
    if ticket.exists():
        sys.exit(0)
    prompt = arguments[arguments.index('-p') + 1]
    sys.stderr.write(prompt)
    sys.stderr.flush()
    if sys.stdin.buffer.readline() != b'sudo-test-password\\n':
        sys.stderr.write(prompt)
        sys.stderr.flush()
        sys.exit(1)
    ticket.touch()
    sys.exit(0)
while arguments[0].startswith('-'):
    if arguments[0] == '--':
        arguments = arguments[1:]
        break
    arguments = arguments[1:]
if not ticket.exists():
    sys.exit(1)
os.execvp(arguments[0], arguments)
"""


def directory_contents(directory: Path) -> dict[str, bytes]:
    """Capture file contents and relative paths to compare a whole mirror."""
    contents = {}
    for path in directory.rglob("*"):
        if path.is_file():
            contents[str(path.relative_to(directory))] = path.read_bytes()
    return contents


class DestinationTests(unittest.TestCase):
    def test_destination_should_refuse_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "point de montage autre que /"):
            backup.validate_destination(Path("/"), [])

    def test_destination_should_refuse_unmounted_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("backup.os.path.ismount", return_value=False):
                with self.assertRaisesRegex(ValueError, "point de montage"):
                    backup.validate_destination(Path(directory), [])

    def test_destination_should_refuse_missing_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("backup.os.path.ismount", return_value=True):
                with self.assertRaisesRegex(ValueError, "répertoire attendu absent"):
                    backup.validate_destination(Path(directory), ["home"])

    def test_destination_should_refuse_symlink_to_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside").mkdir()
            (root / "home").symlink_to(root / "outside", target_is_directory=True)
            with patch("backup.os.path.ismount", return_value=True):
                with self.assertRaisesRegex(ValueError, "lien symbolique"):
                    backup.validate_destination(root, ["home"])

    def test_destination_should_refuse_a_readonly_mount_before_a_real_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("backup.os.path.ismount", return_value=True):
                with patch("backup.os.statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    with self.assertRaisesRegex(ValueError, "lecture seule"):
                        backup.validate_destination(Path(directory), [], require_writable=True)

    def test_destination_should_allow_a_readonly_mount_for_a_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("backup.os.path.ismount", return_value=True):
                with patch("backup.os.statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    destination = backup.validate_destination(Path(directory), [], require_writable=False)
        self.assertEqual(destination, Path(directory))


class SourceDirectoryTests(unittest.TestCase):
    def test_source_directory_should_refuse_root_with_any_number_of_slashes(self) -> None:
        for directory in ("/", "//", "///"):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "différent de /"):
                    backup.remote_directory(directory)


@unittest.skipUnless(shutil.which("rsync") is not None, "rsync doit être installé")
class MirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="backup test ")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.destination = self.root / "disk"
        self.destination.mkdir()
        self.sources = {}
        for name in ("home", "nextcloud", "config"):
            source = self.root / "server" / name
            source.mkdir(parents=True)
            (source / "fichier avec espaces é.txt").write_text(f"nouveau {name}", encoding="utf-8")
            (source / ".hidden").write_text("caché", encoding="utf-8")
            (source / ".hidden").chmod(0o640)
            os.utime(source / ".hidden", (1_700_000_000, 1_700_000_000))
            self.sources[name] = source
            target = self.destination / name
            target.mkdir()
            (target / "obsolete.txt").write_text("ancien", encoding="utf-8")
        (self.destination / "unrelated.txt").write_text("conserver", encoding="utf-8")

        binary_directory = self.root / "bin"
        binary_directory.mkdir()
        fake_ssh = binary_directory / "ssh"
        fake_ssh.write_text(FAKE_SSH, encoding="utf-8")
        fake_ssh.chmod(0o755)
        fake_sudo = binary_directory / "sudo"
        fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
        fake_sudo.chmod(0o755)
        environment = dict(os.environ)
        environment["PATH"] = f"{binary_directory}{os.pathsep}{os.environ['PATH']}"
        environment.pop("SUDO_USER", None)
        environment_patch = patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

        self.options = backup.parse_arguments([
            "mirror", "fake-server", str(self.destination), "--no-remote-sudo",
            "--home-dir", str(self.sources["home"]),
            "--data-dir", str(self.sources["nextcloud"]),
            "--config-dir", str(self.sources["config"]),
        ])

    def run_mirror(self) -> None:
        with patch("backup.os.path.ismount", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                backup.run_mirror(self.options)

    def test_mirror_should_copy_and_delete_with_the_existing_layout(self) -> None:
        self.run_mirror()

        for name, source in self.sources.items():
            with self.subTest(name=name):
                self.assertEqual(directory_contents(self.destination / name), directory_contents(source))
                self.assertEqual(
                    (self.destination / name / ".hidden").stat().st_mtime,
                    (source / ".hidden").stat().st_mtime,
                )
                self.assertEqual(
                    stat.S_IMODE((self.destination / name / ".hidden").stat().st_mode),
                    0o640,
                )
        self.assertEqual((self.destination / "unrelated.txt").read_text(), "conserver")

    def test_mirror_should_work_with_the_default_remote_sudo_transport(self) -> None:
        self.options.no_remote_sudo = False

        self.run_mirror()

        for name, source in self.sources.items():
            with self.subTest(name=name):
                self.assertEqual(directory_contents(self.destination / name), directory_contents(source))

    def test_mirror_should_authenticate_sudo_without_corrupting_real_rsync_transfers(self) -> None:
        (self.root / "bin" / "sudo").write_text(PASSWORD_SUDO, encoding="utf-8")
        self.options.host = "remote-user@fake-server"
        with contextlib.closing(CredentialSession({"sudo-remote": "sudo-test-password"})) as session:
            self.options.no_remote_sudo = False
            self.options.remote_sudo_password = True
            self.options.ssh_control_path = str(session.control_path)
            environment = {SOCKET_ENVIRONMENT_VARIABLE: str(session.socket_path),
                           "BACKUP_TEST_SUDO_TICKET": str(self.root / "sudo-ticket")}
            with patch.dict(os.environ, environment):
                self.run_mirror()

        for name, source in self.sources.items():
            with self.subTest(name=name):
                self.assertEqual(directory_contents(self.destination / name), directory_contents(source))

    def test_mirror_should_support_an_explicit_remote_username(self) -> None:
        self.options.host = "remote-user@fake-server"

        self.run_mirror()

        for name, source in self.sources.items():
            with self.subTest(name=name):
                self.assertEqual(directory_contents(self.destination / name), directory_contents(source))

    def test_mirror_should_leave_the_disk_unchanged_when_dry_run_is_requested(self) -> None:
        before = directory_contents(self.destination)
        before_paths = sorted(str(path) for path in self.destination.rglob("*"))
        self.options.dry_run = True

        self.run_mirror()

        self.assertEqual(directory_contents(self.destination), before)
        self.assertEqual(sorted(str(path) for path in self.destination.rglob("*")), before_paths)

    def test_dry_run_should_not_create_a_deletion_buffer_when_many_files_would_be_deleted(self) -> None:
        target = self.destination / "home"
        for number in range(1200):
            (target / f"obsolete_{number:04d}_{'x' * 100}").touch()
        self.options.home_only = True
        self.options.dry_run = True
        self.options.progress = True
        library = ctypes.CDLL(None, use_errno=True)
        library.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        descriptor = library.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        self.assertGreaterEqual(descriptor, 0)
        self.addCleanup(os.close, descriptor)
        watch = library.inotify_add_watch(descriptor, os.fsencode(target), 0x100 | 0x200 | 0x2)
        self.assertGreaterEqual(watch, 0)
        actual_run = subprocess.run
        outputs = []

        def quiet_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            result = actual_run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
            if command[0] == "rsync":
                outputs.append(result.stdout.decode("utf-8"))
            return result

        with patch("backup.subprocess.run", side_effect=quiet_run):
            self.run_mirror()

        try:
            events = os.read(descriptor, 1_000_000)
        except BlockingIOError:
            events = b""
        self.assertEqual(events, b"", "La simulation ne doit créer, modifier ou supprimer aucun fichier.")
        self.assertIn("*deleting", outputs[0])
        self.assertNotIn("%", outputs[0], "Une simulation ne doit pas afficher de faux débits de transfert.")

    def test_mirror_should_leave_other_targets_unchanged_when_home_only_is_requested(self) -> None:
        nextcloud_before = directory_contents(self.destination / "nextcloud")
        config_before = directory_contents(self.destination / "config")
        self.options.home_only = True

        self.run_mirror()

        self.assertEqual(directory_contents(self.destination / "home"), directory_contents(self.sources["home"]))
        self.assertEqual(directory_contents(self.destination / "nextcloud"), nextcloud_before)
        self.assertEqual(directory_contents(self.destination / "config"), config_before)

    def test_mirror_should_refuse_every_transfer_when_a_remote_source_is_missing(self) -> None:
        before = directory_contents(self.destination)
        self.options.data_dir = str(self.root / "missing")

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_mirror()

        self.assertEqual(directory_contents(self.destination), before)

    def test_mirror_should_refuse_every_transfer_when_a_remote_source_is_empty(self) -> None:
        before = directory_contents(self.destination)
        for path in self.sources["nextcloud"].iterdir():
            path.unlink()

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_mirror()

        self.assertEqual(directory_contents(self.destination), before)

    def test_mirror_should_handle_shell_characters_in_remote_paths(self) -> None:
        unusual_directory = self.root / "server" / "data 'quoted'; $unused"
        self.sources["nextcloud"].rename(unusual_directory)
        self.options.data_dir = str(unusual_directory)

        self.run_mirror()

        self.assertEqual(
            directory_contents(self.destination / "nextcloud"),
            directory_contents(unusual_directory),
        )

    def test_mirror_should_stop_when_rsync_fails(self) -> None:
        before = directory_contents(self.destination)
        with patch("backup.run_remote_script"):
            with patch("backup.subprocess.run", side_effect=subprocess.CalledProcessError(23, "rsync")) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_mirror()

        self.assertEqual(run.call_count, 1)
        self.assertEqual(directory_contents(self.destination), before)

    def test_interrupted_transfer_should_keep_the_old_file_and_resume_from_the_partial(self) -> None:
        source_data = bytes(range(256)) * 8192
        (self.sources["home"] / "payload.bin").write_bytes(source_data)
        target = self.destination / "home" / "payload.bin"
        target.write_bytes(b"ancienne version complete")
        transport = self.root / "bin" / "ssh"
        transport.write_text(INTERRUPTING_SSH, encoding="utf-8")

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_mirror()

        self.assertEqual(target.read_bytes(), b"ancienne version complete")
        partial = target.parent / ".rsync-partial" / "payload.bin"
        self.assertTrue(partial.is_file())
        self.assertGreater(partial.stat().st_size, 0)
        self.assertLess(partial.stat().st_size, len(source_data))
        self.assertTrue((target.parent / "obsolete.txt").exists())

        transport.write_text(FAKE_SSH, encoding="utf-8")
        self.run_mirror()

        self.assertEqual(target.read_bytes(), source_data)
        self.assertFalse(partial.exists())
        self.assertFalse((target.parent / "obsolete.txt").exists())

    def test_cancellation_should_prevent_the_next_phase_even_if_the_current_transfer_succeeds(self) -> None:
        nextcloud_before = directory_contents(self.destination / "nextcloud")
        config_before = directory_contents(self.destination / "config")
        actual_run = subprocess.run
        with contextlib.closing(CredentialSession({})) as session:
            def run_and_cancel(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
                result = actual_run(command, **kwargs)
                if command[0] == "rsync":
                    session.cancel()
                return result

            with patch.dict(os.environ, {SOCKET_ENVIRONMENT_VARIABLE: str(session.socket_path)}):
                with patch("backup.subprocess.run", side_effect=run_and_cancel):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_mirror()

        self.assertEqual(directory_contents(self.destination / "home"), directory_contents(self.sources["home"]))
        self.assertEqual(directory_contents(self.destination / "nextcloud"), nextcloud_before)
        self.assertEqual(directory_contents(self.destination / "config"), config_before)


class PreparationTests(unittest.TestCase):
    def test_preparation_should_not_connect_when_dry_run_is_requested(self) -> None:
        options = backup.parse_arguments(["prepare-nextcloud", "server", "--dry-run"])

        with patch("backup.run_remote_script") as remote:
            with contextlib.redirect_stdout(io.StringIO()):
                backup.prepare_nextcloud(options)

        remote.assert_not_called()

    def test_ssh_should_use_the_original_user_when_launched_with_sudo(self) -> None:
        options = backup.parse_arguments(["prepare-nextcloud", "server"])
        with patch.dict(os.environ, {"SUDO_USER": "backup-user"}):
            with patch("backup.os.geteuid", return_value=0):
                command = backup.ssh_command(options)

        self.assertEqual(command[:4], ["sudo", "-n", "-u", "backup-user"])
        self.assertIn("--preserve-env=SSH_AUTH_SOCK,BACKUP_CREDENTIAL_SOCKET", command)
        self.assertIn("ssh", command)


if __name__ == "__main__":
    unittest.main()
