"""End-to-end recording checks against a local, controllable TCP sensor."""

import csv
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from sensor_client import CSV_COLUMNS, SensorClient, parse_sample


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for acquisition state")


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.server.settimeout(3)
        self.client = SensorClient()
        self.peer = None

    def tearDown(self):
        self.client.close()
        wait_for(lambda: not self.client.is_alive())
        if self.peer is not None:
            self.peer.close()
        self.server.close()
        self.folder.cleanup()

    def connect(self, greeting=b"# KX134"):
        self.client.connect("127.0.0.1", self.server.getsockname()[1])
        self.peer, _ = self.server.accept()
        self.peer.settimeout(3)
        self.peer.sendall(greeting + b"\r\nread_ms,x_g,y_g,z_g\r\n1,0.1,-0.2,1.0\r\n")
        wait_for(lambda: self.client.snapshot().received_samples == 1)

    def test_fragmented_stream_start_stop_and_disconnect(self):
        self.connect()
        self.client.start_recording(Path(self.folder.name), "trial-01")
        wait_for(lambda: self.client.snapshot().recording)
        first_path = self.client.snapshot().recording_path
        self.assertEqual(first_path.name, "trial-01.csv")

        # A split row and many rows per packet exercise actual TCP framing.
        rows = b"".join(f"{i},0.1234,-0.5000,1.0000\r\n".encode() for i in range(100, 200))
        self.peer.sendall(rows[:7])
        self.peer.sendall(rows[7:513])
        self.peer.sendall(rows[513:])
        self.peer.sendall(b"bad,row\r\n200,nan,0,1\r\n# sensor note\r\n")
        wait_for(lambda: self.client.snapshot().recorded_samples == 100
                 and self.client.snapshot().invalid_lines == 2)
        self.client.stop_recording()
        wait_for(lambda: not self.client.snapshot().recording)

        with first_path.open(newline="") as stream:
            saved = list(csv.DictReader(stream))
        self.assertEqual(tuple(saved[0]), CSV_COLUMNS)
        self.assertEqual([int(row["read_ms"]) for row in saved], list(range(100, 200)))
        self.assertTrue(all(float(row["x_g"]) == 0.1234 for row in saved))
        self.assertTrue(all(row["received_at_utc"].endswith("+00:00") for row in saved))

        # Stop closes the file but does not stop live acquisition.
        bytes_after_stop = first_path.read_bytes()
        self.peer.sendall(b"201,0,0,1\r\n")
        wait_for(lambda: self.client.snapshot().received_samples == 102)
        self.assertEqual(first_path.read_bytes(), bytes_after_stop)

        # Reusing a name (with or without the extension) must not replace a trial.
        self.client.start_recording(Path(self.folder.name), "trial-01.csv")
        kind, message = self.client.events.get(timeout=3)
        self.assertEqual(kind, "error")
        self.assertIn("already exists", message)
        self.assertFalse(self.client.snapshot().recording)
        self.assertEqual(first_path.read_bytes(), bytes_after_stop)

        # A new recording has its own file and is closed on peer disconnect.
        self.client.start_recording(Path(self.folder.name))
        wait_for(lambda: self.client.snapshot().recording)
        second_path = self.client.snapshot().recording_path
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(second_path.name.startswith("kx134_"))
        self.peer.sendall(b"202,0,0,1\r\n")
        wait_for(lambda: self.client.snapshot().recorded_samples == 1)
        self.peer.close()
        self.peer = None
        wait_for(lambda: self.client.snapshot().phase == "disconnected")
        self.assertFalse(self.client.snapshot().recording)
        with second_path.open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

        # Reconnection clears live counts without overwriting prior trials.
        self.connect()
        self.assertEqual(len(list(Path(self.folder.name).glob("*.csv"))), 2)

    def test_window_close_finishes_active_recording(self):
        self.connect()
        self.client.start_recording(Path(self.folder.name))
        wait_for(lambda: self.client.snapshot().recording)
        path = self.client.snapshot().recording_path
        self.peer.sendall(b"2,0.1,0.2,0.3\r\n")
        wait_for(lambda: self.client.snapshot().recorded_samples == 1)
        self.client.close()
        wait_for(lambda: not self.client.is_alive())
        with path.open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_scrolling_plot_history_does_not_trim_recording(self):
        self.connect()
        self.client.start_recording(Path(self.folder.name))
        wait_for(lambda: self.client.snapshot().recording)
        path = self.client.snapshot().recording_path

        # Thirty seconds of source timestamps exceed the ten-second plot view.
        timestamps = list(range(100, 30_100, 20))
        self.peer.sendall(b"".join(f"{ms},0.1,0.2,1.0\r\n".encode() for ms in timestamps))
        wait_for(lambda: self.client.snapshot().recorded_samples == len(timestamps))
        wait_for(lambda: self.client.plot_snapshot().points[-1][1].read_ms == timestamps[-1])
        history = self.client.plot_snapshot()
        self.assertEqual(len(history.points), 501)
        self.assertEqual(history.points[-1][0], timestamps[-1] - timestamps[0])
        self.assertEqual(history.points[-1][0] - history.points[0][0], 10_000)

        self.client.stop_recording()
        wait_for(lambda: not self.client.snapshot().recording)
        with path.open(newline="") as stream:
            self.assertEqual([int(row["read_ms"]) for row in csv.DictReader(stream)], timestamps)

        self.client.start_recording(Path(self.folder.name))
        wait_for(lambda: self.client.snapshot().recording)
        self.peer.sendall(b"30100,0,0,1\r\n")
        wait_for(lambda: self.client.snapshot().recorded_samples == 1)
        wait_for(lambda: bool(self.client.plot_snapshot().points))
        self.assertEqual(self.client.plot_snapshot().points[0][0], 0)

    def test_range_confirmation_and_recording(self):
        self.connect(b"# RANGE 64")
        for old, new in ((64, 8), (8, 16), (16, 32), (32, 64)):
            with self.subTest(range_g=new):
                self.client.set_range(new)
                with self.peer.makefile("rb") as commands:
                    self.assertEqual(commands.readline(), f"RANGE {new}\n".encode())
                self.assertEqual(self.client.snapshot().active_range_g, old)
                self.assertEqual(self.client.snapshot().pending_range_g, new)
                self.client.start_recording(Path(self.folder.name), "too-early")
                self.assertIn("confirm", self.client.events.get(timeout=3)[1])
                self.assertFalse(self.client.snapshot().recording)

                # Old samples can precede the reply in the stream; the new
                # range starts a fresh plot when the complete reply arrives.
                self.peer.sendall(b"2,0.1,0.2,1.0\r\n# RAN")
                self.peer.sendall(f"GE {new}\r\n3,0.1,0.2,1.0\r\n".encode())
                wait_for(lambda: self.client.snapshot().active_range_g == new
                         and len(self.client.plot_snapshot().points) == 1)
                wait_for(lambda: self.client.plot_snapshot().points[0][1].read_ms == 3)
                self.assertEqual(self.client.plot_snapshot().points[0][0], 0)
                self.assertIsNone(self.client.snapshot().pending_range_g)

        self.client.start_recording(Path(self.folder.name), "confirmed")
        wait_for(lambda: self.client.snapshot().recording)
        self.client.set_range(8)
        self.assertIsNone(self.client.snapshot().pending_range_g)
        self.peer.sendall(b"4,0.1,0.2,1.0\r\n")
        wait_for(lambda: self.client.snapshot().recorded_samples == 1)
        self.client.stop_recording()
        wait_for(lambda: not self.client.snapshot().recording)
        with (Path(self.folder.name) / "confirmed.csv").open(newline="") as stream:
            self.assertEqual([row["range_g"] for row in csv.DictReader(stream)], ["64"])

    def test_range_error_and_missing_confirmation(self):
        self.connect(b"# RANGE 64")
        self.client.set_range(8)
        with self.peer.makefile("rb") as commands:
            self.assertEqual(commands.readline(), b"RANGE 8\n")
            self.peer.sendall(b"# RANGE ERROR Previous range restored.\r\n")
            self.assertIn("restored", self.client.events.get(timeout=3)[1])
            self.assertEqual(self.client.snapshot().active_range_g, 64)
            self.assertIsNone(self.client.snapshot().pending_range_g)
            with patch("sensor_client.RANGE_TIMEOUT", 0.25):
                self.client.set_range(16)
                self.assertEqual(commands.readline(), b"RANGE 16\n")
                self.assertIn("did not confirm", self.client.events.get(timeout=3)[1])
        self.assertEqual(self.client.snapshot().phase, "disconnected")
        self.assertIsNone(self.client.snapshot().active_range_g)
        self.peer.close()
        self.connect(b"# RANGE 16")
        self.assertEqual(self.client.snapshot().active_range_g, 16)

    def test_legacy_board_remains_recordable(self):
        self.connect(b"# KX134; range=+/-64 g; acceleration in g; configured ODR=50 Hz")
        self.assertEqual(self.client.snapshot().active_range_g, 64)
        self.assertFalse(self.client.snapshot().range_control)
        self.client.set_range(8)
        self.assertIsNone(self.client.snapshot().pending_range_g)
        self.client.start_recording(Path(self.folder.name))
        wait_for(lambda: self.client.snapshot().recording)


class ProtocolTests(unittest.TestCase):
    def test_timestamp_rollover_values_are_preserved(self):
        self.assertEqual(parse_sample("4294967295,0,0,1").read_ms, 4294967295)
        self.assertEqual(parse_sample("0,0,0,1").read_ms, 0)
        with self.assertRaises(ValueError):
            parse_sample("4294967296,0,0,1")


if __name__ == "__main__":
    unittest.main()
