"""Exercise transport backpressure without SSH or a remote server."""

import errno
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from ssh_transport import forward_input, relay, SudoHandshake, write_output


class TransportOutputTests(unittest.TestCase):
    def test_output_should_preserve_every_byte_when_a_nonblocking_pipe_fills(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        os.set_blocking(reader, False)
        os.set_blocking(writer, False)
        received = bytearray()
        payload = bytes(range(256)) * 4096
        stream = Mock()
        stream.fileno.return_value = writer

        def drain_pipe(readers: list, writers: list, errors: list) -> tuple:
            self.assertEqual((readers, writers, errors), ([], [writer], []))
            received.extend(os.read(reader, len(payload)))
            return [], [writer], []

        # Each wait drains a real full pipe, without sleeps or a reader race.
        with patch("ssh_transport.select.select", side_effect=drain_pipe) as wait:
            write_output(stream, payload)

        received.extend(os.read(reader, len(payload)))
        self.assertGreater(wait.call_count, 0)
        self.assertEqual(received, payload)
        stream.write.assert_not_called()

    def test_output_should_report_a_broken_pipe_instead_of_retrying_forever(self) -> None:
        reader, writer = os.pipe()
        os.close(reader)
        self.addCleanup(os.close, writer)
        stream = Mock()
        stream.fileno.return_value = writer

        with self.assertRaisesRegex(BrokenPipeError, "Broken pipe"):
            write_output(stream, b"protocol")

    def test_output_should_fail_when_a_write_makes_no_progress(self) -> None:
        with patch("ssh_transport.os.write", return_value=0):
            with self.assertRaisesRegex(OSError, "sans progression"):
                write_output(Mock(), b"protocol")


class TransportInputTests(unittest.TestCase):
    def test_input_should_wait_instead_of_closing_when_a_nonblocking_pipe_is_empty(self) -> None:
        remote_input = Mock()
        with patch("ssh_transport.os.read", side_effect=[
            BlockingIOError(errno.EAGAIN, "temporarily unavailable"), b"protocol", b"",
        ]):
            with patch("ssh_transport.select.select") as wait:
                forward_input(remote_input)

        wait.assert_called_once_with([sys.stdin.fileno()], [], [])
        remote_input.write.assert_called_once_with(b"protocol")
        remote_input.flush.assert_called_once()
        remote_input.close.assert_called_once()

    def test_input_should_close_cleanly_when_ssh_exits_during_a_write(self) -> None:
        remote_input = Mock()
        remote_input.write.side_effect = BrokenPipeError("SSH closed")
        remote_input.close.side_effect = BrokenPipeError("SSH closed")
        with patch("ssh_transport.os.read", return_value=b"protocol"):
            forward_input(remote_input)

        remote_input.close.assert_called_once()


class RelayTests(unittest.TestCase):
    def test_relay_should_kill_unresponsive_ssh_and_preserve_the_original_error(self) -> None:
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 5), 0]
        selector = Mock()
        key = Mock()
        key.fileobj = process.stderr
        selector.select.return_value = [(key, None)]
        with patch("ssh_transport.subprocess.Popen") as launch:
            launch.return_value.__enter__.return_value = process
            with patch("ssh_transport.selectors.DefaultSelector", return_value=selector):
                with patch("ssh_transport.os.read", return_value=b"PROMPT"):
                    with patch("ssh_transport.request_secret", return_value=""):
                        with self.assertRaisesRegex(ValueError, "Renseigner le mot de passe"):
                            relay(["ssh"], SudoHandshake(b"PROMPT", b"READY"))

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        selector.close.assert_called_once()

    def test_relay_should_preserve_binary_data_with_nonblocking_standard_pipes(self) -> None:
        command = """
import os
import sys
from ssh_transport import relay, SudoHandshake

for descriptor in (0, 1, 2):
    os.set_blocking(descriptor, False)
remote = (
    "import sys; "
    "sys.stderr.buffer.write(b'READY'); sys.stderr.buffer.flush(); "
    "data = sys.stdin.buffer.read(); "
    "sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); "
    "sys.stderr.buffer.write(b'diagnostic' * 16384)"
)
sys.exit(relay([sys.executable, '-c', remote], SudoHandshake(b'PROMPT', b'READY')))
"""
        payload = bytes(range(256)) * 16384

        result = subprocess.run(
            [sys.executable, "-c", command], input=payload,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parent.parent, timeout=15, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"diagnostic" * 16384)


if __name__ == "__main__":
    unittest.main()
