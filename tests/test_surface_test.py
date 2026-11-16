"""Verify restart checkpoints without accessing any physical disk."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import surface_test as surface


def smart_document(serial: str = "test-disk", status: int = 118) -> dict:
    return {
        "model_name": surface.EXPECTED_MODEL,
        "serial_number": serial,
        "user_capacity": {"bytes": surface.EXPECTED_SIZE},
        "logical_block_size": 512,
        "ata_smart_data": {"self_test": {"status": {"value": status}}},
        "ata_smart_attributes": {"table": [{"id": 197, "raw": {"value": 22088}}]},
    }


def disk_document(arguments: list[str], mountpoint: str | None) -> dict:
    """Model lsblk's flat default when NAME and --tree are both absent."""
    disk = {
        "path": "/dev/test",
        "type": "disk",
        "size": surface.EXPECTED_SIZE,
        "mountpoints": [None],
        "maj:min": "8:32",
    }
    partition = {
        "path": "/dev/test1",
        "type": "part",
        "mountpoints": [mountpoint],
        "maj:min": "8:33",
    }
    if "--tree" in arguments:
        disk["children"] = [partition]
        return {"blockdevices": [disk]}
    return {"blockdevices": [disk, partition]}


class SurfaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.identity = surface.parse_smart(smart_document()).identity
        self.state = surface.ScanState(version=1, identity=self.identity, zone_bytes=1024 * 1024)
        self.stop = surface.StopRequest()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_parse_smart_should_reject_a_different_model(self) -> None:
        document = smart_document()
        document["model_name"] = "another disk"

        with self.assertRaisesRegex(surface.SurfaceTestError, "Disque refusé"):
            surface.parse_smart(document)

    def test_parse_smart_should_require_the_actual_ata_serial(self) -> None:
        with self.assertRaisesRegex(surface.SurfaceTestError, "Identité ATA"):
            surface.parse_smart(smart_document(serial=""))

    def test_parse_smart_should_reject_a_running_self_test(self) -> None:
        with self.assertRaisesRegex(surface.SurfaceTestError, "autotest SMART est encore actif"):
            surface.parse_smart(smart_document(status=249))

    def test_prepare_state_should_reject_another_disk_with_the_same_model(self) -> None:
        surface.save_state(self.directory, self.state)
        other_identity = surface.parse_smart(smart_document(serial="other-test-disk")).identity

        with self.assertRaisesRegex(surface.SurfaceTestError, "autre disque"):
            surface.prepare_state(self.directory, other_identity, None)

    def test_prepare_state_should_keep_existing_zone_boundaries(self) -> None:
        surface.save_state(self.directory, self.state)

        with self.assertRaisesRegex(surface.SurfaceTestError, "taille des zones"):
            surface.prepare_state(self.directory, self.identity, 2)

    def test_prepare_state_should_reject_orphaned_logs(self) -> None:
        (self.directory / "journal.jsonl").touch()

        with self.assertRaisesRegex(surface.SurfaceTestError, "sans état de reprise"):
            surface.prepare_state(self.directory, self.identity, None)

    def test_load_state_should_reject_a_checkpoint_inside_a_zone(self) -> None:
        surface.save_state(self.directory, replace(self.state, next_block=1))

        with self.assertRaisesRegex(surface.SurfaceTestError, "État de reprise invalide"):
            surface.load_state(self.directory)

    def test_require_unused_disk_should_reject_a_mounted_child_partition(self) -> None:
        node = {
            "type": "disk",
            "mountpoints": [None],
            "maj:min": "8:32",
            "children": [{"type": "part", "mountpoints": ["/media/disk"], "maj:min": "8:33"}],
        }

        with (
            patch.object(Path, "is_dir", return_value=True),
            patch.object(Path, "iterdir", return_value=iter(())),
        ):
            with self.assertRaisesRegex(surface.SurfaceTestError, "monté"):
                surface.require_unused_disk(node)

    def test_require_unused_disk_should_reject_unknown_mount_status(self) -> None:
        with self.assertRaisesRegex(surface.SurfaceTestError, "points de montage"):
            surface.require_unused_disk({"type": "disk"})

    def test_inspect_disk_should_reject_a_regular_file_before_running_commands(self) -> None:
        scratch_file = self.directory / "scratch.bin"
        scratch_file.touch()

        with patch.object(surface, "read_command_json") as reader:
            with self.assertRaisesRegex(surface.SurfaceTestError, "pas un fichier"):
                surface.inspect_disk(scratch_file)

        reader.assert_not_called()

    def test_inspect_disk_should_reject_an_unexpected_capacity(self) -> None:
        scratch_file = self.directory / "scratch.bin"
        scratch_file.touch()
        document = {"blockdevices": [{"type": "disk", "size": 4096}]}

        with patch.object(surface.stat, "S_ISBLK", return_value=True):
            with patch.object(surface, "read_command_json", return_value=document):
                with self.assertRaisesRegex(surface.SurfaceTestError, "capacité exposée"):
                    surface.inspect_disk(scratch_file)

    def inspect_simulated_disk(self, mountpoint: str | None) -> surface.SmartSnapshot:
        device = self.directory / "device"
        device.touch()

        def read_diagnostic(arguments: list[str]) -> dict:
            if arguments[0] == "lsblk":
                return disk_document(arguments, mountpoint)
            return smart_document()

        with (
            patch.object(surface.stat, "S_ISBLK", return_value=True),
            patch.object(surface, "read_command_json", side_effect=read_diagnostic),
            patch.object(Path, "is_dir", return_value=True),
            patch.object(Path, "iterdir", return_value=iter(())),
        ):
            return surface.inspect_disk(device)

    def test_inspect_disk_should_accept_a_whole_disk_with_an_unmounted_partition(self) -> None:
        snapshot = self.inspect_simulated_disk(mountpoint=None)

        self.assertEqual(snapshot.identity, self.identity)

    def test_inspect_disk_should_reject_a_mounted_partition_below_the_whole_disk(self) -> None:
        with self.assertRaisesRegex(surface.SurfaceTestError, "monté"):
            self.inspect_simulated_disk(mountpoint="/media/disk")

    def test_exclusive_lock_should_reject_a_second_process_lock(self) -> None:
        with surface.exclusive_lock(self.directory / "lock"):
            with self.assertRaisesRegex(surface.SurfaceTestError, "autre test"):
                with surface.exclusive_lock(self.directory / "lock"):
                    self.fail("Concurrent test acquired the lock")

    def test_scan_zones_should_repeat_the_interrupted_zone_after_a_restart(self) -> None:
        calls = []

        def interrupt_second_zone(zone: surface.Zone) -> surface.ZoneResult:
            calls.append(zone)
            if len(calls) == 2:
                raise surface.ScanInterrupted
            return surface.ZoneResult(0, 0, 0, 0)

        surface.save_state(self.directory, self.state)
        with self.assertRaises(surface.ScanInterrupted):
            surface.scan_zones(self.state, self.directory, interrupt_second_zone, self.stop)

        checkpoint = surface.load_state(self.directory)
        resumed_zones = []

        def finish_zone(zone: surface.Zone) -> surface.ZoneResult:
            resumed_zones.append(zone)
            return surface.ZoneResult(0, 0, 0, 0)

        resumed = surface.scan_zones(
            checkpoint, self.directory, finish_zone, self.stop, max_zones=1
        )

        self.assertEqual(checkpoint.next_block, 256)
        self.assertEqual(resumed_zones, [calls[1]])
        self.assertEqual(resumed.next_block, 512)

    def test_scan_zones_should_preserve_checkpoint_when_stop_arrives_after_the_child(self) -> None:
        def stop_after_child(zone: surface.Zone) -> surface.ZoneResult:
            self.stop.requested = True
            return surface.ZoneResult(0, 0, 0, 0)

        surface.save_state(self.directory, self.state)

        with self.assertRaises(surface.ScanInterrupted):
            surface.scan_zones(self.state, self.directory, stop_after_child, self.stop)

        self.assertEqual(surface.load_state(self.directory).next_block, 0)

    def test_scan_zones_should_keep_the_previous_checkpoint_when_device_validation_fails(
        self,
    ) -> None:
        def disconnected_device(zone: surface.Zone) -> surface.ZoneResult:
            raise surface.SurfaceTestError("device disconnected")

        surface.save_state(self.directory, self.state)

        with self.assertRaisesRegex(surface.SurfaceTestError, "device disconnected"):
            surface.scan_zones(self.state, self.directory, disconnected_device, self.stop)

        self.assertEqual(surface.load_state(self.directory), self.state)

    def test_scan_zones_should_finish_as_interrupted_when_a_diagnostic_fails_during_stop(
        self,
    ) -> None:
        def interrupted_diagnostic(zone: surface.Zone) -> surface.ZoneResult:
            self.stop.requested = True
            raise surface.SurfaceTestError("diagnostic interrupted")

        surface.save_state(self.directory, self.state)

        with self.assertRaises(surface.ScanInterrupted):
            surface.scan_zones(self.state, self.directory, interrupted_diagnostic, self.stop)

        self.assertEqual(surface.load_state(self.directory), self.state)

    def test_scan_zones_should_record_errors_without_repeating_completed_zones(self) -> None:
        state = surface.scan_zones(
            self.state,
            self.directory,
            lambda zone: surface.ZoneResult(2, 2, 0, 0),
            self.stop,
            max_zones=1,
        )

        self.assertEqual(surface.load_state(self.directory), state)
        self.assertEqual((state.next_block, state.bad_blocks, state.zones_with_errors), (256, 2, 1))
        records = [
            json.loads(line) for line in (self.directory / "journal.jsonl").read_text().splitlines()
        ]
        self.assertEqual(records[-1]["read_errors"], 2)
        self.assertNotIn("test-disk", (self.directory / "state.json").read_text())

    def test_scan_zones_should_finish_the_current_zone_when_the_time_limit_is_reached(self) -> None:
        ticks = iter((0, 0, 61))
        calls = []

        def finish_zone(zone: surface.Zone) -> surface.ZoneResult:
            calls.append(zone)
            return surface.ZoneResult(0, 0, 0, 0)

        state = surface.scan_zones(
            self.state,
            self.directory,
            finish_zone,
            self.stop,
            max_minutes=1,
            clock=lambda: next(ticks),
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(state.next_block, 256)

    def test_scan_zones_should_limit_the_last_zone_to_the_device_size(self) -> None:
        zone_blocks = self.state.zone_bytes // surface.BLOCK_SIZE
        next_block = (self.state.total_blocks // zone_blocks) * zone_blocks
        state = replace(self.state, next_block=next_block)
        zones = []

        def finish_zone(zone: surface.Zone) -> surface.ZoneResult:
            zones.append(zone)
            return surface.ZoneResult(0, 0, 0, 0)

        state = surface.scan_zones(state, self.directory, finish_zone, self.stop)

        self.assertEqual(zones[0].last_block, state.total_blocks - 1)
        self.assertEqual(state.next_block, state.total_blocks)

    def test_save_state_should_preserve_the_previous_checkpoint_when_replace_fails(self) -> None:
        surface.save_state(self.directory, self.state)
        newer_state = replace(self.state, next_block=256)

        with patch.object(surface.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaisesRegex(OSError, "simulated failure"):
                surface.save_state(self.directory, newer_state)

        self.assertEqual(surface.load_state(self.directory), self.state)

    def test_read_zone_result_should_reject_an_interrupted_log_even_with_a_completion_line(
        self,
    ) -> None:
        log = self.directory / "zone.log"
        bad_blocks = self.directory / "zone.badblocks"
        log.write_text(
            "Interrupted at block 0\nPass completed, 0 bad blocks found. (0/0/0 errors)\n"
        )
        bad_blocks.write_text("")

        with self.assertRaisesRegex(surface.SurfaceTestError, "Fin de zone non confirmée"):
            surface.read_zone_result(log, bad_blocks, surface.Zone(0, 256))

    def test_read_zone_result_should_keep_bad_blocks_when_the_command_completes(self) -> None:
        log = self.directory / "zone.log"
        bad_blocks = self.directory / "zone.badblocks"
        log.write_text("Pass completed, 2 bad blocks found. (1/1/0 errors)\n")
        bad_blocks.write_text("12\n13\n")

        result = surface.read_zone_result(log, bad_blocks, surface.Zone(0, 256))

        self.assertEqual(result, surface.ZoneResult(2, 1, 1, 0))

    def test_checked_zone_should_not_write_when_disk_identity_changes(self) -> None:
        other_identity = surface.parse_smart(smart_document(serial="other-disk")).identity

        with patch.object(
            surface, "inspect_disk", return_value=surface.SmartSnapshot(other_identity, {})
        ):
            with patch.object(surface, "run_badblocks") as writer:
                with self.assertRaisesRegex(
                    surface.SurfaceTestError, "identité du disque a changé"
                ):
                    surface.checked_zone(
                        Path("/dev/test"),
                        -1,
                        self.identity,
                        surface.Zone(0, 256),
                        self.directory,
                        self.stop,
                    )

        writer.assert_not_called()

    def test_run_badblocks_should_not_validate_a_cancelled_child_even_when_it_exits_successfully(
        self,
    ) -> None:
        child_code = """
import os
import signal
import sys

def stop(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.alarm(10)
os.kill(os.getppid(), signal.SIGINT)
signal.pause()
"""
        original_popen = surface.subprocess.Popen

        def start_child(arguments: list[str], **options: object) -> surface.subprocess.Popen[bytes]:
            return original_popen([sys.executable, "-c", child_code], **options)

        with patch.object(surface.subprocess, "Popen", side_effect=start_child):
            with surface.stop_handlers(self.stop):
                with self.assertRaises(surface.ScanInterrupted):
                    surface.run_badblocks(
                        Path("/dev/unused"), surface.Zone(0, 256), self.directory, self.stop
                    )

        self.assertTrue(self.stop.requested)
        self.assertIsNone(self.stop.child)

    @unittest.skipUnless(shutil.which("badblocks"), "badblocks is not installed")
    def test_run_badblocks_should_write_and_verify_only_the_requested_zone_on_a_scratch_file(
        self,
    ) -> None:
        scratch_file = self.directory / "scratch.bin"
        original_block = b"x" * surface.BLOCK_SIZE
        scratch_file.write_bytes(original_block * 4)

        result = surface.run_badblocks(scratch_file, surface.Zone(1, 2), self.directory, self.stop)

        self.assertEqual(result, surface.ZoneResult(0, 0, 0, 0))
        self.assertEqual(scratch_file.read_bytes(), original_block + b"\0" * 8192 + original_block)


if __name__ == "__main__":
    unittest.main()
