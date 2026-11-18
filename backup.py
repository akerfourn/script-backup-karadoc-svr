#!/usr/bin/env python3
"""Mirror remote server directories onto an already mounted backup disk."""

import argparse
import getpass
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path, PurePosixPath

from credentials import CredentialSession, request_secret, SOCKET_ENVIRONMENT_VARIABLE


PROJECT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATA_DIRECTORY = "/var/snap/nextcloud/common/nextcloud/data"
DEFAULT_CONFIG_DIRECTORY = "/home/kerfadmin/nextcloud"
DEFAULT_LITECLOUD_DATA_DIRECTORY = "/var/lib/litecloud/data"
LITECLOUD_DATABASE = "litecloud.sqlite3"
LITECLOUD_EXCLUDED_FILES = (
    LITECLOUD_DATABASE, f"{LITECLOUD_DATABASE}-wal", f"{LITECLOUD_DATABASE}-shm",
    f"{LITECLOUD_DATABASE}-journal", ".instance.lock",
)

SOURCE_CHECK_SCRIPT = """\
set -euo pipefail
for source_directory in "$@"; do
    if [[ ! -d "$source_directory" ]]; then
        printf 'Répertoire source absent : %s\\n' "$source_directory" >&2
        exit 1
    fi
    first_entry=$(find "$source_directory" -mindepth 1 -maxdepth 1 -print -quit)
    if [[ -z "$first_entry" ]]; then
        printf 'Source vide, copie refusée : %s\\n' "$source_directory" >&2
        exit 1
    fi
done
"""

LITECLOUD_SOURCE_CHECK_SCRIPT = """\
set -euo pipefail
users_directory=$1/users
if [[ -d "$users_directory" && ! -L "$users_directory" ]]; then
    for account_directory in "$users_directory"/*; do
        if [[ ${account_directory##*/} =~ ^[0-9a-f]{64}$ &&
              ! -L "$account_directory" &&
              -d "$account_directory/files" && ! -L "$account_directory/files" ]]; then
            exit 0
        fi
    done
fi
printf 'Aucun dossier utilisateur LiteCloud valide sous %s, copie refusée.\\n' "$users_directory" >&2
exit 1
"""


def remote_host(value: str) -> str:
    """Validate an SSH alias or a user@hostname destination."""
    if value.startswith("-") or re.fullmatch(r"[a-zA-Z0-9_.@-]+", value) is None:
        raise argparse.ArgumentTypeError("utiliser un alias SSH ou utilisateur@serveur")
    return value


def remote_directory(value: str) -> str:
    """Require an explicit absolute source directory other than the root."""
    directory = PurePosixPath(value)
    if not directory.is_absolute() or directory == PurePosixPath(directory.anchor):
        raise argparse.ArgumentTypeError("le chemin doit être absolu et différent de /")
    if ".." in directory.parts or "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError("le chemin ne doit contenir ni '..' ni saut de ligne")
    return str(directory)


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    """Parse connection settings and the requested backup operation."""
    connection = argparse.ArgumentParser(add_help=False)
    connection.add_argument("host", type=remote_host, help="alias SSH ou utilisateur@serveur")
    connection.add_argument("--ssh-as", help="utilisateur SSH local (défaut : SUDO_USER)")
    connection.add_argument(
        "--interactive-ssh", action="store_true", help="autoriser les demandes de mot de passe SSH"
    )
    connection.add_argument(
        "--interactive-sudo", action="store_true", help="saisir le mot de passe sudo distant"
    )
    connection.add_argument("--ssh-control-path", help="connexion SSH déjà ouverte par l'interface")
    connection.add_argument(
        "--remote-sudo-password", action="store_true",
        help="utiliser le mot de passe sudo distant de la session graphique",
    )
    connection.add_argument(
        "--no-remote-sudo", action="store_true", help="ne pas utiliser sudo sur le serveur"
    )
    connection.add_argument("--dry-run", action="store_true", help="simuler sans écrire")
    connection.add_argument(
        "--config-dir", type=remote_directory, default=DEFAULT_CONFIG_DIRECTORY,
        help="répertoire distant contenant dump.sql, config.php et apps/",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    mirror_parser = commands.add_parser("mirror", parents=[connection], help="copier le miroir")
    mirror_parser.add_argument("destination", type=Path, help="point de montage local du disque")
    mirror_modes = mirror_parser.add_mutually_exclusive_group()
    mirror_modes.add_argument("--home-only", action="store_true", help="copier uniquement /home")
    mirror_modes.add_argument("--litecloud", action="store_true", help="copier les fichiers LiteCloud")
    mirror_parser.add_argument(
        "--no-delete", action="store_true", help="conserver les fichiers absents du serveur"
    )
    mirror_parser.add_argument("--progress", action="store_true", help="afficher la progression rsync")
    mirror_parser.add_argument("--home-dir", type=remote_directory, default="/home")
    mirror_parser.add_argument(
        "--data-dir", type=remote_directory, help="répertoire de données distant du cloud"
    )
    mirror_parser.add_argument(
        "--prepare-nextcloud", action="store_true", help="rafraîchir le dump avant la copie"
    )
    commands.add_parser(
        "prepare-nextcloud", parents=[connection], help="rafraîchir le dump sur le serveur"
    )
    database_parser = commands.add_parser(
        "backup-litecloud-db", parents=[connection], help="sauvegarder SQLite à chaud"
    )
    database_parser.add_argument("destination", type=Path, help="point de montage local du disque")
    database_parser.add_argument(
        "--data-dir", type=remote_directory, default=DEFAULT_LITECLOUD_DATA_DIRECTORY,
        help="répertoire LiteCloud distant",
    )
    options = parser.parse_args(arguments)
    if options.command == "mirror":
        if options.home_only and options.prepare_nextcloud:
            parser.error("--home-only et --prepare-nextcloud sont incompatibles")
        if options.litecloud and options.prepare_nextcloud:
            parser.error("--litecloud et --prepare-nextcloud sont incompatibles")
        if options.data_dir is None:
            options.data_dir = DEFAULT_DATA_DIRECTORY
            if options.litecloud:
                options.data_dir = DEFAULT_LITECLOUD_DATA_DIRECTORY
    if options.remote_sudo_password and (options.ssh_control_path is None or options.no_remote_sudo):
        parser.error("--remote-sudo-password nécessite --ssh-control-path et sudo distant")
    if options.interactive_sudo and (options.no_remote_sudo or options.ssh_control_path is not None):
        parser.error("--interactive-sudo nécessite sudo distant et ouvre sa propre connexion SSH")
    return options


def local_ssh_user(options: argparse.Namespace) -> str | None:
    """Identify the local account that supplies SSH configuration and credentials."""
    if options.ssh_as is not None:
        return options.ssh_as
    return os.environ.get("SUDO_USER")


def ssh_command(options: argparse.Namespace) -> list[str]:
    """Keep the invoking user's SSH configuration when launched through sudo."""
    if options.remote_sudo_password:
        command = [
            sys.executable, str(PROJECT_DIRECTORY / "ssh_transport.py"),
            "--control-path", options.ssh_control_path,
        ]
    else:
        batch_mode = "yes"
        if options.interactive_ssh:
            batch_mode = "no"
        command = [
            "ssh", "-o", f"BatchMode={batch_mode}", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        ]
        if options.ssh_control_path is not None:
            command.extend(["-S", options.ssh_control_path, "-o", "ControlMaster=no"])
    ssh_user = local_ssh_user(options)
    if ssh_user is not None and ssh_user != "root":
        if os.geteuid() != 0:
            raise ValueError("--ssh-as nécessite un lancement avec sudo")
        command = [
            "sudo", "-n", "-u", ssh_user,
            f"--preserve-env=SSH_AUTH_SOCK,{SOCKET_ENVIRONMENT_VARIABLE}", "--", *command
        ]
    return command


def prompt_remote_sudo_password(host: str) -> str:
    """Read a password only when the terminal can hide it."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(f"Mot de passe sudo distant ({host}) : ")
        except (EOFError, getpass.GetPassWarning) as error:
            raise ValueError("--interactive-sudo nécessite un terminal interactif.") from error


@contextmanager
def interactive_sudo_session(options: argparse.Namespace) -> Iterator[argparse.Namespace]:
    """Reuse one SSH login and the private sudo credential socket for the CLI."""
    ssh = ssh_command(options)
    ssh_user = local_ssh_user(options)
    owner_uid = None
    if ssh_user is not None:
        try:
            owner_uid = pwd.getpwnam(ssh_user).pw_uid
        except KeyError as error:
            raise ValueError(f"Compte SSH local introuvable : {ssh_user}") from error

    previous_socket = os.environ.get(SOCKET_ENVIRONMENT_VARIABLE)
    password = prompt_remote_sudo_password(options.host)
    with closing(CredentialSession({"sudo-remote": password}, owner_uid=owner_uid)) as credentials:
        control_path = str(credentials.control_path)
        master = [*ssh, "-S", control_path, "-M", "-N", "-f", "-o", "ControlPersist=60"]
        try:
            subprocess.run([*master, "--", options.host], check=True)
            os.environ[SOCKET_ENVIRONMENT_VARIABLE] = str(credentials.socket_path)
            connected = argparse.Namespace(**vars(options))
            connected.ssh_control_path = control_path
            connected.remote_sudo_password = True
            yield connected
        finally:
            if previous_socket is None:
                os.environ.pop(SOCKET_ENVIRONMENT_VARIABLE, None)
            else:
                os.environ[SOCKET_ENVIRONMENT_VARIABLE] = previous_socket
            try:
                subprocess.run(
                    [*ssh, "-S", control_path, "-O", "exit", "--", options.host],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"Fermeture SSH : {error}", file=sys.stderr)


def remote_script_command(options: argparse.Namespace, interpreter: list[str]) -> list[str]:
    """Build a remote script command with individually quoted arguments."""
    remote_command = interpreter
    if not options.no_remote_sudo:
        remote_command = ["sudo", "-n", "--", *remote_command]
    return [*ssh_command(options), "--", options.host, shlex.join(remote_command)]


def run_remote_script(
    options: argparse.Namespace, script: str, arguments: list[str]
) -> None:
    """Run a fixed Bash script remotely."""
    command = remote_script_command(options, ["bash", "-s", "--", *arguments])
    subprocess.run(command, input=script, text=True, check=True)


def prepare_nextcloud(options: argparse.Namespace) -> None:
    """Refresh the historical Nextcloud export on the remote server."""
    if options.dry_run:
        print("Simulation : le dump et la configuration distants ne seront pas modifiés.", flush=True)
        return
    script = (PROJECT_DIRECTORY / "lib" / "prepare-nextcloud.sh").read_text(encoding="utf-8")
    print("Préparation du dump et de la configuration Nextcloud sur le serveur…", flush=True)
    run_remote_script(options, script, [options.config_dir])


def mirror_sources(options: argparse.Namespace) -> list[tuple[str, str]]:
    """Map remote sources to the existing disk layout."""
    if options.litecloud:
        return [(options.data_dir, "litecloud")]
    sources = [(options.home_dir, "home")]
    if not options.home_only:
        sources.extend([(options.data_dir, "nextcloud"), (options.config_dir, "config")])
    return sources


def check_cancelled() -> None:
    """Observe GUI cancellation before starting another remote operation."""
    if SOCKET_ENVIRONMENT_VARIABLE in os.environ:
        if request_secret("cancelled") == "yes":
            raise KeyboardInterrupt


def validate_destination(
    destination: Path,
    names: list[str],
    require_writable: bool = False,
    allow_missing: bool = False,
    create_missing: bool = False,
) -> Path:
    """Validate the mounted disk and its expected destination directories."""
    destination = destination.expanduser().resolve(strict=True)
    if destination == Path("/") or not os.path.ismount(destination):
        raise ValueError(f"la destination doit être un point de montage autre que / : {destination}")
    if require_writable and os.statvfs(destination).f_flag & os.ST_RDONLY:
        raise ValueError(f"le disque de destination est monté en lecture seule : {destination}")
    for name in names:
        directory = destination / name
        if directory.is_symlink():
            raise ValueError(
                f"le répertoire de destination ne peut pas être un lien symbolique : {directory}"
            )
        if directory.exists():
            if not directory.is_dir():
                raise ValueError(f"la destination existe mais n'est pas un répertoire : {directory}")
            continue
        if create_missing:
            directory.mkdir(mode=0o755)
            continue
        if not allow_missing:
            raise ValueError(f"répertoire attendu absent : {directory}")
    return destination


def run_mirror(options: argparse.Namespace) -> None:
    """Check all sources before mirroring them sequentially over SSH."""
    sources = mirror_sources(options)
    destination_names = [name for _, name in sources]
    destination = validate_destination(
        options.destination,
        destination_names,
        require_writable=not options.dry_run,
        allow_missing=True,
    )
    check_cancelled()
    if options.prepare_nextcloud:
        prepare_nextcloud(options)
    check_cancelled()
    source_check = SOURCE_CHECK_SCRIPT
    if options.litecloud:
        source_check = LITECLOUD_SOURCE_CHECK_SCRIPT
    run_remote_script(options, source_check, [source for source, _ in sources])

    if not options.dry_run:
        validate_destination(
            destination,
            destination_names,
            require_writable=True,
            create_missing=True,
        )

    remote_rsync = "sudo -n rsync"
    if options.no_remote_sudo:
        remote_rsync = "rsync"
    transport = shlex.join(ssh_command(options))
    for source, name in sources:
        check_cancelled()
        validate_destination(
            destination,
            destination_names,
            require_writable=not options.dry_run,
            allow_missing=options.dry_run,
        )
        print(f"Miroir : {options.host}:{source}/ → {destination / name}/", flush=True)
        command = [
            "rsync", "-av", "--numeric-ids",
            "--partial", "--partial-dir=.rsync-partial",
            "--protect-args", "--itemize-changes", f"--rsync-path={remote_rsync}",
            "-e", transport,
        ]
        if options.litecloud:
            command.extend(f"--exclude=/{filename}" for filename in LITECLOUD_EXCLUDED_FILES)
        if options.dry_run:
            command.append("--dry-run")
        if not options.no_delete:
            # delete-delay may write a temporary deletion list even in dry-run mode.
            deletion_option = "--delete-during" if options.dry_run else "--delete-delay"
            command.append(deletion_option)
        if options.progress and not options.dry_run:
            command.extend(["--info=progress2", "--outbuf=L"])
        command.extend(["--", f"{options.host}:{source}/", f"{destination / name}/"])
        subprocess.run(command, check=True)
    check_cancelled()


def backup_litecloud_database(options: argparse.Namespace) -> None:
    """Receive and verify a standalone SQLite snapshot before publishing it."""
    destination = validate_destination(
        options.destination, ["litecloud"], require_writable=not options.dry_run
    )
    directory = destination / "litecloud"
    if directory.stat().st_dev != destination.stat().st_dev:
        raise ValueError("le dossier litecloud doit être sur le disque de sauvegarde")
    snapshot = directory / LITECLOUD_DATABASE
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(directory / f"{LITECLOUD_DATABASE}{suffix}"):
            raise ValueError(f"journal SQLite présent à côté de la sauvegarde : {suffix}")
    if options.dry_run:
        print(f"Simulation : sauvegarde SQLite prévue vers {snapshot}", flush=True)
        return

    script = (PROJECT_DIRECTORY / "lib" / "export-litecloud-sqlite.py").read_bytes()
    source = str(PurePosixPath(options.data_dir) / LITECLOUD_DATABASE)
    command = remote_script_command(options, ["python3", "-", source])
    print("Export SQLite LiteCloud à chaud…", flush=True)
    with tempfile.TemporaryDirectory(prefix=".litecloud-db-", dir=destination) as staging:
        pending = Path(staging) / LITECLOUD_DATABASE
        with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            subprocess.run(command, input=script, stdout=output, check=True)
            output.flush()
            os.fsync(output.fileno())
        with closing(sqlite3.connect(pending.as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
            if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("le contrôle d’intégrité de la copie SQLite a échoué")
            database.execute("SELECT id, username FROM users LIMIT 0")
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.replace(pending, snapshot.name, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    print(f"Base SQLite vérifiée : {snapshot}", flush=True)


def main(arguments: list[str] | None = None) -> int:
    """Run the CLI and stop on the first failure."""
    options = parse_arguments(arguments)
    try:
        programs = ("ssh",)
        if options.command != "backup-litecloud-db":
            programs = ("ssh", "rsync")
        for program in programs:
            if shutil.which(program) is None:
                raise ValueError(f"commande requise introuvable : {program}")
        operation = prepare_nextcloud
        if options.command == "mirror":
            if os.geteuid() != 0:
                raise ValueError("lancer avec sudo pour conserver propriétaires et permissions")
            operation = run_mirror
        elif options.command == "backup-litecloud-db":
            operation = backup_litecloud_database
        authentication = nullcontext(options)
        if options.interactive_sudo and (options.command == "mirror" or not options.dry_run):
            authentication = interactive_sudo_session(options)
        with authentication as connected_options:
            operation(connected_options)
    except subprocess.CalledProcessError as error:
        if error.returncode == 255:
            print(f"Erreur SSH : tester « ssh {options.host} » sans sudo.", file=sys.stderr)
            if not options.interactive_ssh:
                print("BatchMode=yes interdit les demandes de mot de passe. Utiliser l'interface, "
                      "--interactive-ssh ou une clé SSH chargée dans l'agent.", file=sys.stderr)
        else:
            print(f"Commande échouée (code {error.returncode}) ; voir le message précédent.", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCopie interrompue ; relancer la même commande pour reprendre.", file=sys.stderr)
        return 130
    print("Terminé.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
