#!/usr/bin/env python3
"""Authenticate remote sudo before forwarding the binary rsync protocol."""

import argparse
import os
import select
import secrets
import selectors
import shlex
import subprocess
import sys
import threading
from typing import BinaryIO

from credentials import request_secret


def write_output(stream: BinaryIO, data: bytes) -> None:
    """Forward every byte, waiting when an inherited nonblocking pipe is full."""
    descriptor = stream.fileno()
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except BlockingIOError:
            select.select([], [descriptor], [])
            continue
        if written == 0:
            raise OSError("Écriture du transport interrompue sans progression.")
        remaining = remaining[written:]


class SudoHandshake:
    """Separate sudo prompts from stderr without touching protocol bytes."""

    def __init__(self, prompt: bytes, ready: bytes) -> None:
        self.prompt = prompt
        self.ready = ready
        self.buffer = b""
        self.is_ready = False
        self.password_sent = False

    def feed(self, data: bytes, input_stream: BinaryIO) -> bytes:
        """Consume markers, answer at most once and return ordinary stderr."""
        self.buffer += data
        if self.prompt in self.buffer:
            if self.password_sent:
                raise ValueError("Mot de passe sudo distant refusé.")
            password = request_secret("sudo-remote")
            if password == "":
                raise ValueError("Renseigner le mot de passe sudo distant.")
            input_stream.write(password.encode("utf-8") + b"\n")
            input_stream.flush()
            self.password_sent = True
            self.buffer = self.buffer.replace(self.prompt, b"", 1)
        if self.ready in self.buffer:
            self.buffer = self.buffer.replace(self.ready, b"", 1)
            self.is_ready = True
        if self.is_ready:
            output = self.buffer
            self.buffer = b""
            return output
        # Keep a possible split marker; avoid retaining arbitrary stderr output.
        suffix_length = max(len(self.prompt), len(self.ready)) - 1
        output = self.buffer[:-suffix_length]
        self.buffer = self.buffer[-suffix_length:]
        return output


def forward_input(input_stream: BinaryIO) -> None:
    """Start forwarding stdin only after sudo has finished reading its password."""
    try:
        while True:
            descriptor = sys.stdin.fileno()
            try:
                data = os.read(descriptor, 65536)
            except BlockingIOError:
                select.select([descriptor], [], [])
                continue
            if data == b"":
                break
            input_stream.write(data)
            input_stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            input_stream.close()
        except BrokenPipeError:
            # Closing a buffered pipe may retry data after SSH has exited.
            pass


def privileged_command(remote_command: str, prompt: str, ready: str) -> str:
    """Keep both sudo calls under the same remote shell for its credential cache."""
    script = (
        f"sudo -S -p {shlex.quote(prompt)} -v || exit 1\n"
        f"printf '%s' {shlex.quote(ready)} >&2\n"
        f"{remote_command}\n"
        "command_status=$?\n"
        'exit "$command_status"\n'
    )
    return "bash -c " + shlex.quote(script)


def relay(command: list[str], handshake: SudoHandshake) -> int:
    """Relay stdout and stderr while keeping authentication off the data stream."""
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, sys.stdout.buffer)
        selector.register(process.stderr, selectors.EVENT_READ, sys.stderr.buffer)
        input_started = False
        try:
            while selector.get_map():
                for key, _ in selector.select():
                    data = os.read(key.fd, 65536)
                    if data == b"":
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stderr and not handshake.is_ready:
                        data = handshake.feed(data, process.stdin)
                    # Avoid Python buffers: a failed flush can retain protocol bytes
                    # and retry them during interpreter shutdown.
                    write_output(key.data, data)
                if handshake.is_ready and not input_started:
                    threading.Thread(target=forward_input, args=(process.stdin,), daemon=True).start()
                    input_started = True
            if handshake.buffer:
                write_output(sys.stderr.buffer, handshake.buffer)
            return process.wait()
        except (OSError, ValueError, KeyboardInterrupt):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            selector.close()


def main(arguments: list[str] | None = None) -> int:
    """Act as an rsync remote shell using an existing OpenSSH master connection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-path", required=True)
    parser.add_argument("-l", dest="user", help="utilisateur SSH transmis par rsync")
    parser.add_argument("host")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    marker = secrets.token_hex(16)
    prompt = f"BACKUP_SUDO_PROMPT_{marker}"
    ready = f"BACKUP_SUDO_READY_{marker}"
    remote_command = privileged_command(" ".join(options.command), prompt, ready)
    command = [
        "ssh", "-S", options.control_path, "-o", "ControlMaster=no", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
    ]
    if options.user is not None:
        command.extend(["-l", options.user])
    command.extend(["--", options.host, remote_command])
    try:
        return relay(command, SudoHandshake(prompt.encode(), ready.encode()))
    except ValueError as error:
        print(f"Erreur d'authentification : {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Erreur de transport SSH : {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
