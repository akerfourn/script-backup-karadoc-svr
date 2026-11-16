"""Test destructif du HGST, par zones, avec reprise conservée sur le PC."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_MODEL = "HGST HTS721010A9E630"
EXPECTED_SIZE = 1_000_204_886_016
BLOCK_SIZE = 4096
DEFAULT_STATE_DIRECTORY = Path(__file__).resolve().parent / ".surface-test"
COMPLETED_PASS = re.compile(
    r"Pass completed, (\d+) bad blocks found\. \((\d+)/(\d+)/(\d+) errors\)"
)


class SurfaceTestError(Exception):
    """Le test ne peut pas continuer sans risque de faux résultat."""


class ScanInterrupted(Exception):
    """La zone courante doit être recommencée."""


@dataclass(frozen=True)
class DiskIdentity:
    model: str
    size_bytes: int
    fingerprint: str


@dataclass(frozen=True)
class SmartSnapshot:
    identity: DiskIdentity
    attributes: dict[str, int]


@dataclass(frozen=True)
class ScanState:
    version: int
    identity: DiskIdentity
    zone_bytes: int
    next_block: int = 0
    bad_blocks: int = 0
    zones_with_errors: int = 0

    @property
    def total_blocks(self) -> int:
        return self.identity.size_bytes // BLOCK_SIZE


@dataclass(frozen=True)
class Zone:
    first_block: int
    block_count: int

    @property
    def last_block(self) -> int:
        return self.first_block + self.block_count - 1


@dataclass(frozen=True)
class ZoneResult:
    bad_blocks: int
    read_errors: int
    write_errors: int
    comparison_errors: int


class StopRequest:
    def __init__(self) -> None:
        self.requested = False
        self.child: subprocess.Popen[bytes] | None = None

    def request(self, signum: int, frame: object) -> None:
        first_request = not self.requested
        self.requested = True
        self.interrupt_child()
        if first_request:
            try:
                os.write(
                    sys.stdout.fileno(),
                    "\nArrêt demandé : attente de la fin des E/S en cours…\n".encode(),
                )
            except OSError:
                # Closing a terminal must still interrupt the child if stdout is gone.
                pass

    def interrupt_child(self) -> None:
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass


@contextlib.contextmanager
def stop_handlers(stop: StopRequest) -> Iterator[None]:
    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, stop.request)
    try:
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def read_command_json(arguments: list[str]) -> dict:
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    # smartctl also returns bits for historical SMART failures, not just execution errors.
    if result.returncode < 0 or result.returncode & 3:
        raise SurfaceTestError(f"Échec de {Path(arguments[0]).name} (code {result.returncode}).")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SurfaceTestError("Réponse JSON du diagnostic illisible.") from error
    if not isinstance(document, dict):
        raise SurfaceTestError("Réponse JSON du diagnostic invalide.")
    return document


def parse_smart(document: dict) -> SmartSnapshot:
    try:
        model = document["model_name"].strip()
        serial = document["serial_number"].strip()
        size = document["user_capacity"]["bytes"]
        logical_sector_size = document["logical_block_size"]
        self_test_status = document["ata_smart_data"]["self_test"]["status"]["value"]
    except (KeyError, TypeError, AttributeError) as error:
        raise SurfaceTestError(
            "Identité ATA ou statut de l'autotest indisponible : arrêt."
        ) from error
    if model != EXPECTED_MODEL or size != EXPECTED_SIZE or logical_sector_size != 512:
        raise SurfaceTestError(f"Disque refusé : seul le {EXPECTED_MODEL} de 1 To est autorisé.")
    if not serial or not isinstance(self_test_status, int) or not 0 <= self_test_status <= 255:
        raise SurfaceTestError("Identité ATA ou statut de l'autotest invalide.")
    if self_test_status >> 4 == 15:
        raise SurfaceTestError(
            "Un autotest SMART est encore actif : attendre ou l'arrêter d'abord."
        )
    fingerprint = hashlib.sha256(f"{model}\n{serial}\n{size}".encode()).hexdigest()
    attributes = {}
    for attribute in document.get("ata_smart_attributes", {}).get("table", []):
        if attribute.get("id") in (5, 196, 197, 198, 199):
            raw_value = attribute.get("raw", {}).get("value")
            if isinstance(raw_value, int):
                attributes[str(attribute["id"])] = raw_value
    return SmartSnapshot(DiskIdentity(model, size, fingerprint), attributes)


def require_unused_disk(node: dict) -> None:
    mountpoints = node.get("mountpoints")
    if not isinstance(mountpoints, list):
        raise SurfaceTestError("Impossible de vérifier les points de montage : refus.")
    if any(point is not None for point in mountpoints):
        raise SurfaceTestError("Disque ou partition monté(e), ou utilisé(e) comme swap : refus.")
    if node.get("type") not in ("disk", "part"):
        raise SurfaceTestError(
            "Le disque contient un périphérique actif (RAID, LVM, chiffrement…)."
        )
    device_number = node.get("maj:min")
    if not isinstance(device_number, str) or re.fullmatch(r"\d+:\d+", device_number) is None:
        raise SurfaceTestError("Numéro du périphérique indisponible.")
    holders = Path("/sys/dev/block") / device_number / "holders"
    if not holders.is_dir() or any(holders.iterdir()):
        raise SurfaceTestError("Impossible de confirmer que le disque est libre de tout usage.")
    for child in node.get("children", []):
        require_unused_disk(child)


def inspect_disk(device: Path) -> SmartSnapshot:
    if not stat.S_ISBLK(device.stat().st_mode):
        raise SurfaceTestError("La cible doit être un disque bloc entier, pas un fichier.")
    document = read_command_json(
        [
            "lsblk",
            "--json",
            # Without NAME in the selected columns, lsblk defaults to a flat list.
            "--tree",
            "--bytes",
            "--output",
            "PATH,TYPE,SIZE,MOUNTPOINTS,MAJ:MIN",
            str(device),
        ]
    )
    devices = document.get("blockdevices", [])
    if len(devices) != 1 or devices[0].get("type") != "disk":
        raise SurfaceTestError("La cible doit être un disque entier, pas une partition.")
    if devices[0].get("size") != EXPECTED_SIZE:
        raise SurfaceTestError("La capacité exposée par le périphérique ne correspond pas au HGST.")
    require_unused_disk(devices[0])
    return parse_smart(
        read_command_json(
            [
                "smartctl",
                "-i",
                "-A",
                "-c",
                "-j",
                "-d",
                "sat",
                str(device),
            ]
        )
    )


@contextlib.contextmanager
def exclusive_lock(path: Path, device: bool = False) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if not device:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SurfaceTestError(
                "Un autre test utilise déjà ce disque ou cet état de reprise."
            ) from error
        yield descriptor
    finally:
        os.close(descriptor)


def save_state(directory: Path, state: ScanState) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(state), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, directory / "state.json")
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_state(directory: Path) -> ScanState:
    try:
        document = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        identity = DiskIdentity(**document.pop("identity"))
        state = ScanState(identity=identity, **document)
        numbers = (
            state.version,
            state.zone_bytes,
            state.next_block,
            state.bad_blocks,
            state.zones_with_errors,
        )
        if any(type(number) is not int for number in numbers):
            raise ValueError("compteurs invalides")
        if identity.model != EXPECTED_MODEL or identity.size_bytes != EXPECTED_SIZE:
            raise ValueError("disque inattendu")
        if re.fullmatch(r"[0-9a-f]{64}", identity.fingerprint) is None:
            raise ValueError("empreinte invalide")
        if state.version != 1 or not 1 <= state.zone_bytes // (1024 * 1024) <= 1024:
            raise ValueError("format ou taille de zone invalide")
        if state.zone_bytes % (1024 * 1024) != 0 or not 0 <= state.next_block <= state.total_blocks:
            raise ValueError("position invalide")
        zone_blocks = state.zone_bytes // BLOCK_SIZE
        if state.next_block != state.total_blocks and state.next_block % zone_blocks != 0:
            raise ValueError("position hors frontière de zone")
        completed_zones = (state.next_block + zone_blocks - 1) // zone_blocks
        if not 0 <= state.bad_blocks <= state.next_block:
            raise ValueError("nombre de blocs défectueux invalide")
        if not 0 <= state.zones_with_errors <= completed_zones:
            raise ValueError("nombre de zones défectueuses invalide")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise SurfaceTestError("État de reprise invalide : aucun test lancé.") from error
    return state


def journal(directory: Path, event: str, **details: object) -> None:
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    with (directory / "journal.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def show_progress(state: ScanState) -> None:
    percentage = state.next_block / state.total_blocks * 100
    gibibytes = state.next_block * BLOCK_SIZE / 1024**3
    print(
        f"Parcouru : {percentage:.2f} % ({gibibytes:.2f} Gio) ; "
        f"{state.zones_with_errors} zone(s) avec erreurs, "
        f"{state.bad_blocks} bloc(s) de 4 Kio signalé(s).",
        flush=True,
    )


def read_zone_result(log_path: Path, bad_blocks_path: Path, zone: Zone) -> ZoneResult:
    log = log_path.read_text(encoding="utf-8", errors="replace")
    match = COMPLETED_PASS.search(log)
    if match is None or "Interrupted at block" in log or "aborting test" in log:
        raise SurfaceTestError(f"Fin de zone non confirmée. Voir {log_path}")
    try:
        blocks = {int(line) for line in bad_blocks_path.read_text().splitlines()}
    except ValueError as error:
        raise SurfaceTestError(f"Liste de blocs illisible : {bad_blocks_path}") from error
    if any(block < zone.first_block or block > zone.last_block for block in blocks):
        raise SurfaceTestError("Le rapport contient des blocs hors de la zone testée.")
    counts = [int(value) for value in match.groups()]
    if counts[0] != len(blocks):
        raise SurfaceTestError("Le rapport et la liste des blocs défectueux sont incohérents.")
    return ZoneResult(*counts)


def run_badblocks(
    device: Path,
    zone: Zone,
    directory: Path,
    stop: StopRequest,
    lock_descriptor: int | None = None,
) -> ZoneResult:
    descriptor, log_name = tempfile.mkstemp(
        prefix=f"zone-{zone.first_block:09d}-", suffix=".log", dir=directory
    )
    log_path = Path(log_name)
    bad_blocks_path = log_path.with_suffix(".badblocks")
    arguments = [
        "badblocks",
        "-w",
        "-v",
        "-b",
        str(BLOCK_SIZE),
        "-c",
        "1024",
        "-t",
        "0",
        "-o",
        str(bad_blocks_path),
        str(device),
        str(zone.last_block),
        str(zone.first_block),
    ]
    with os.fdopen(descriptor, "wb") as log:
        journal(
            directory,
            "zone_started",
            first_block=zone.first_block,
            last_block=zone.last_block,
            log=log_path.name,
            bad_blocks_file=bad_blocks_path.name,
        )
        if stop.requested:
            raise ScanInterrupted
        with subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env={**os.environ, "LC_ALL": "C"},
            start_new_session=True,
            # Keep the disk lock held even if the parent is killed unexpectedly.
            pass_fds=() if lock_descriptor is None else (lock_descriptor,),
        ) as child:
            stop.child = child
            try:
                if stop.requested:
                    stop.interrupt_child()
                returncode = child.wait()
            finally:
                stop.child = None
        log.flush()
        os.fsync(log.fileno())
    if stop.requested:
        raise ScanInterrupted
    if returncode != 0:
        raise SurfaceTestError(f"badblocks s'est arrêté (code {returncode}). Voir {log_path}")
    with bad_blocks_path.open("rb") as report:
        os.fsync(report.fileno())
    return read_zone_result(log_path, bad_blocks_path, zone)


def scan_zones(
    state: ScanState,
    directory: Path,
    perform_zone: Callable[[Zone], ZoneResult],
    stop: StopRequest,
    max_zones: int | None = None,
    max_minutes: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ScanState:
    started = clock()
    completed = 0
    while state.next_block < state.total_blocks:
        if stop.requested:
            raise ScanInterrupted
        if max_zones is not None and completed >= max_zones:
            break
        if max_minutes is not None and clock() - started >= max_minutes * 60:
            break
        zone = Zone(
            state.next_block,
            min(state.zone_bytes // BLOCK_SIZE, state.total_blocks - state.next_block),
        )
        print(
            f"Écriture puis vérification : blocs {zone.first_block} à {zone.last_block}…",
            flush=True,
        )
        try:
            result = perform_zone(zone)
        except (SurfaceTestError, OSError, subprocess.TimeoutExpired):
            if stop.requested:
                raise ScanInterrupted from None
            raise
        if stop.requested:
            raise ScanInterrupted
        state = replace(
            state,
            next_block=zone.last_block + 1,
            bad_blocks=state.bad_blocks + result.bad_blocks,
            zones_with_errors=state.zones_with_errors + int(result.bad_blocks > 0),
        )
        journal(
            directory,
            "zone_finished",
            first_block=zone.first_block,
            last_block=zone.last_block,
            **asdict(result),
        )
        save_state(directory, state)
        completed += 1
        show_progress(state)
    return state


def checked_zone(
    device: Path,
    descriptor: int,
    identity: DiskIdentity,
    zone: Zone,
    directory: Path,
    stop: StopRequest,
) -> ZoneResult:
    if (
        inspect_disk(device).identity != identity
        or device.stat().st_rdev != os.fstat(descriptor).st_rdev
    ):
        raise SurfaceTestError("L'identité du disque a changé : arrêt.")
    result = run_badblocks(device, zone, directory, stop, lock_descriptor=descriptor)
    os.fsync(descriptor)
    snapshot = inspect_disk(device)
    if snapshot.identity != identity:
        raise SurfaceTestError("L'identité du disque a changé après le test : zone non validée.")
    journal(directory, "smart", attributes=snapshot.attributes)
    return result


def get_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "device", type=Path, nargs="?", help="disque entier HGST ; son contenu sera effacé"
    )
    parser.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE_DIRECTORY, help="état et journaux sur le PC"
    )
    parser.add_argument(
        "--zone-mib",
        type=int,
        help="taille initiale des zones en Mio (256 par défaut, maximum 1024)",
    )
    parser.add_argument(
        "--max-zones", type=int, help="nombre maximum de zones à traiter cette fois"
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        help="arrêt après la zone en cours une fois cette durée atteinte",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="vérifier la cible et afficher la reprise sans écrire",
    )
    parser.add_argument(
        "--status", action="store_true", help="afficher l'état enregistré sans accéder au disque"
    )
    arguments = parser.parse_args()
    if arguments.zone_mib is not None and not 1 <= arguments.zone_mib <= 1024:
        parser.error("--zone-mib doit être compris entre 1 et 1024")
    if arguments.max_zones is not None and arguments.max_zones <= 0:
        parser.error("--max-zones doit être strictement positif")
    if arguments.max_minutes is not None and (
        not math.isfinite(arguments.max_minutes) or arguments.max_minutes <= 0
    ):
        parser.error("--max-minutes doit être une durée finie strictement positive")
    if not arguments.status and arguments.device is None:
        parser.error("le disque est obligatoire, sauf avec --status")
    return arguments


def prepare_state(directory: Path, identity: DiskIdentity, zone_mib: int | None) -> ScanState:
    if (directory / "state.json").exists():
        state = load_state(directory)
        if state.identity != identity:
            raise SurfaceTestError(
                "Ce journal appartient à un autre disque : aucune écriture effectuée."
            )
        if zone_mib is not None and zone_mib * 1024 * 1024 != state.zone_bytes:
            raise SurfaceTestError("La taille des zones doit rester celle du début du test.")
        return state
    if any(directory.glob("zone-*")) or (directory / "journal.jsonl").exists():
        raise SurfaceTestError(
            "Journaux existants sans état de reprise : refus de recommencer silencieusement."
        )
    if zone_mib is None:
        zone_mib = 256
    return ScanState(version=1, identity=identity, zone_bytes=zone_mib * 1024 * 1024)


def main() -> int:
    arguments = get_arguments()
    directory = arguments.state_dir.resolve()
    try:
        if arguments.status:
            show_progress(load_state(directory))
            return 0
        if os.geteuid() != 0:
            raise SurfaceTestError("Lancer ce test avec sudo pour accéder au disque brut.")
        for command in ("badblocks", "smartctl", "lsblk"):
            if shutil.which(command) is None:
                raise SurfaceTestError(f"Commande manquante : {command}.")
        device = arguments.device.resolve(strict=True)
        snapshot = inspect_disk(device)
        state = prepare_state(directory, snapshot.identity, arguments.zone_mib)
        print(f"Disque : {device} ({snapshot.identity.model}). Journaux : {directory}", flush=True)
        print(
            f"Zones de {state.zone_bytes // 1024 // 1024} Mio. SMART brut : {snapshot.attributes}",
            flush=True,
        )
        show_progress(state)
        if arguments.dry_run:
            print("Simulation : aucune écriture effectuée.")
            return 0
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with exclusive_lock(device, device=True) as descriptor, exclusive_lock(directory / "lock"):
            # Reload under the lock so a concurrent launch cannot reuse an obsolete checkpoint.
            snapshot = inspect_disk(device)
            state = prepare_state(directory, snapshot.identity, arguments.zone_mib)
            save_state(directory, state)
            journal(
                directory, "session_started", device=str(device), attributes=snapshot.attributes
            )
            print(
                "TEST DESTRUCTIF : les zones traitées, partitions comprises, "
                "sont remplacées par des zéros.",
                flush=True,
            )
            stop = StopRequest()
            try:
                with stop_handlers(stop):
                    state = scan_zones(
                        state,
                        directory,
                        lambda zone: checked_zone(
                            device, descriptor, snapshot.identity, zone, directory, stop
                        ),
                        stop,
                        arguments.max_zones,
                        arguments.max_minutes,
                    )
            except ScanInterrupted:
                journal(directory, "session_interrupted")
                print("Arrêt terminé. La zone interrompue sera recommencée au prochain lancement.")
                return 130
            journal(directory, "session_finished", next_block=state.next_block)
        if state.next_block == state.total_blocks:
            print(
                "Parcours complet terminé. Consulter les erreurs et le SMART "
                "avant de conclure sur le disque."
            )
        else:
            print("Session terminée. Relancer la même commande pour continuer.")
        if state.bad_blocks:
            return 2
        return 0
    except (SurfaceTestError, OSError, subprocess.TimeoutExpired) as error:
        print(
            f"Erreur : {error}\nLa dernière zone non enregistrée sera recommencée.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
