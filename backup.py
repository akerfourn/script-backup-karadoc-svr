#!/usr/bin/env python3
"""Mirror remote server directories onto an already mounted backup disk."""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

from credentials import request_secret, SOCKET_ENVIRONMENT_VARIABLE


PROJECT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATA_DIRECTORY = "/var/snap/nextcloud/common/nextcloud/data"
DEFAULT_CONFIG_DIRECTORY = "/home/kerfadmin/nextcloud"

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
    mirror_parser.add_argument("--home-only", action="store_true", help="copier uniquement /home")
    mirror_parser.add_argument("--progress", action="store_true", help="afficher la progression rsync")
    mirror_parser.add_argument("--home-dir", type=remote_directory, default="/home")
    mirror_parser.add_argument(
        "--data-dir", type=remote_directory, default=DEFAULT_DATA_DIRECTORY
    )
    mirror_parser.add_argument(
        "--prepare-nextcloud", action="store_true", help="rafraîchir le dump avant la copie"
    )
    commands.add_parser(
        "prepare-nextcloud", parents=[connection], help="rafraîchir le dump sur le serveur"
    )
    options = parser.parse_args(arguments)
    if options.command == "mirror" and options.home_only and options.prepare_nextcloud:
        parser.error("--home-only et --prepare-nextcloud sont incompatibles")
    if options.remote_sudo_password and (options.ssh_control_path is None or options.no_remote_sudo):
        parser.error("--remote-sudo-password nécessite --ssh-control-path et sudo distant")
    return options


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
    ssh_user = options.ssh_as
    if ssh_user is None:
        ssh_user = os.environ.get("SUDO_USER")
    if ssh_user is not None and ssh_user != "root":
        if os.geteuid() != 0:
            raise ValueError("--ssh-as nécessite un lancement avec sudo")
        command = [
            "sudo", "-n", "-u", ssh_user,
            f"--preserve-env=SSH_AUTH_SOCK,{SOCKET_ENVIRONMENT_VARIABLE}", "--", *command
        ]
    return command


def run_remote_script(
    options: argparse.Namespace, script: str, arguments: list[str]
) -> None:
    """Run a fixed script remotely, quoting each argument for the SSH shell."""
    remote_command = ["bash", "-s", "--", *arguments]
    if not options.no_remote_sudo:
        remote_command = ["sudo", "-n", "--", *remote_command]
    command = [*ssh_command(options), "--", options.host, shlex.join(remote_command)]
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
    run_remote_script(options, SOURCE_CHECK_SCRIPT, [source for source, _ in sources])

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
        if options.dry_run:
            # delete-delay may write a temporary deletion list even in dry-run mode.
            command.extend(["--dry-run", "--delete-during"])
        else:
            command.append("--delete-delay")
        if options.progress and not options.dry_run:
            command.extend(["--info=progress2", "--outbuf=L"])
        command.extend(["--", f"{options.host}:{source}/", f"{destination / name}/"])
        subprocess.run(command, check=True)
    check_cancelled()


def main(arguments: list[str] | None = None) -> int:
    """Run the CLI and stop on the first failure."""
    options = parse_arguments(arguments)
    try:
        for program in ("ssh", "rsync"):
            if shutil.which(program) is None:
                raise ValueError(f"commande requise introuvable : {program}")
        if options.command == "mirror":
            if os.geteuid() != 0:
                raise ValueError("lancer avec sudo pour conserver propriétaires et permissions")
            run_mirror(options)
        else:
            prepare_nextcloud(options)
    except subprocess.CalledProcessError as error:
        if error.returncode == 255:
            print(f"Erreur SSH : tester « ssh {options.host} » sans sudo.", file=sys.stderr)
            if not options.interactive_ssh:
                print("BatchMode=yes interdit les demandes de mot de passe. Utiliser l'interface, "
                      "--interactive-ssh ou une clé SSH chargée dans l'agent.", file=sys.stderr)
        else:
            print(f"Commande échouée (code {error.returncode}) ; voir le message précédent.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCopie interrompue ; relancer la même commande pour reprendre.", file=sys.stderr)
        return 130
    print("Terminé.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
