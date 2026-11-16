"""Exchange session credentials over a private local Unix socket."""

import json
import os
import socket
import socketserver
import tempfile
import threading
from pathlib import Path


SOCKET_ENVIRONMENT_VARIABLE = "BACKUP_CREDENTIAL_SOCKET"
MAX_MESSAGE_SIZE = 65536


class CredentialHandler(socketserver.StreamRequestHandler):
    """Answer a single credential request without logging its content."""

    def handle(self) -> None:
        self.request.settimeout(5)
        request = json.loads(self.rfile.readline(MAX_MESSAGE_SIZE))
        purpose = request.get("purpose")
        secret = self.server.take_secret(purpose)
        response = json.dumps({"secret": secret}).encode("utf-8") + b"\n"
        self.wfile.write(response)


class CredentialServer(socketserver.ThreadingUnixStreamServer):
    """Serve the credentials kept in memory by the desktop application."""

    daemon_threads = True

    def __init__(self, path: str, secrets: dict[str, str]) -> None:
        self.secrets = secrets.copy()
        self.requested: set[str] = set()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        super().__init__(path, CredentialHandler)

    def take_secret(self, purpose: str) -> str:
        """Avoid retrying the same rejected SSH or local sudo password."""
        if purpose == "cancelled":
            if self.cancelled.is_set():
                return "yes"
            return "no"
        with self.lock:
            if purpose in ("ssh", "sudo-local"):
                if purpose in self.requested:
                    return ""
                self.requested.add(purpose)
            return self.secrets.get(purpose, "")


class CredentialSession:
    """Own a private socket and remove it when the operation ends."""

    def __init__(self, secrets: dict[str, str]) -> None:
        for secret in secrets.values():
            if "\n" in secret or "\r" in secret or "\x00" in secret:
                raise ValueError("Un mot de passe ne doit pas contenir de saut de ligne ou de NUL.")
        self.directory = tempfile.TemporaryDirectory(prefix="backup-auth-")
        self.socket_path = Path(self.directory.name) / "credentials.sock"
        self.control_path = Path(self.directory.name) / "ssh.sock"
        self.server = CredentialServer(str(self.socket_path), secrets)
        self.socket_path.chmod(0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        """Prevent the receiving process from starting another transfer phase."""
        self.server.cancelled.set()

    def close(self) -> None:
        """Stop requests, discard references to credentials and remove sockets."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server.secrets.clear()
        self.directory.cleanup()


def request_secret(purpose: str) -> str:
    """Read one credential from the current local session."""
    socket_path = os.environ.get(SOCKET_ENVIRONMENT_VARIABLE)
    if socket_path is None:
        raise ValueError("Session d'authentification absente ; lancer l'interface graphique.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(socket_path)
        connection.sendall(json.dumps({"purpose": purpose}).encode("utf-8") + b"\n")
        with connection.makefile("rb") as response:
            result = json.loads(response.readline(MAX_MESSAGE_SIZE))
    secret = result["secret"]
    if not isinstance(secret, str) or "\n" in secret or "\r" in secret or "\x00" in secret:
        raise ValueError("Réponse d'authentification invalide.")
    return secret
