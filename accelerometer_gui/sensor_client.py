"""TCP acquisition and CSV recording for the BIOM 8209 KX134 readout test.

One background thread owns the socket and recording file. GUI callbacks submit
commands, and the GUI reads immutable snapshots. Display refreshes never decide
which received samples are saved. Range commands share the TCP connection.
"""

from __future__ import annotations

import csv
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from pathlib import Path
import queue
import re
import socket
import threading
import time
from typing import TextIO


CSV_COLUMNS = ("received_at_utc", "read_ms", "x_g", "y_g", "z_g", "range_g")
SENSOR_RANGES = (8, 16, 32, 64)
RANGE_REPORT = re.compile(r"# RANGE (8|16|32|64)")
LEGACY_RANGE = re.compile(r"# KX134; range=\+/-([0-9]+) g;")
CONNECT_TIMEOUT = 3.0
RECEIVE_TIMEOUT = 0.1
NO_DATA_TIMEOUT = 5.0
RANGE_TIMEOUT = 3.0
MAX_LINE_BYTES = 4096
PLOT_WINDOW_MS = 10_000
MAX_PLOT_SAMPLES = 5_000


@dataclass(frozen=True)
class Sample:
    received_at_utc: str
    received_monotonic: float
    read_ms: int
    x_g: float
    y_g: float
    z_g: float

    def csv_row(self, range_g: int | None) -> tuple:
        return (self.received_at_utc, self.read_ms, self.x_g, self.y_g, self.z_g, range_g)


def parse_sample(line: str) -> Sample | None:
    """Skip firmware comments/headers and validate a complete sensor CSV row."""
    line = line.strip()
    if not line or line.startswith("#") or line == "read_ms,x_g,y_g,z_g":
        return None
    fields = next(csv.reader([line]))
    if len(fields) != 4:
        raise ValueError("Expected read_ms,x_g,y_g,z_g")
    read_ms = int(fields[0])
    axes = tuple(float(value) for value in fields[1:])
    if not 0 <= read_ms <= 0xFFFFFFFF or not all(map(math.isfinite, axes)):
        raise ValueError("Invalid timestamp or non-finite acceleration")
    return Sample(
        datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        time.monotonic(), read_ms, *axes,
    )


@dataclass(frozen=True)
class Snapshot:
    phase: str = "disconnected"
    status: str = "Disconnected"
    latest: Sample | None = None
    received_samples: int = 0
    invalid_lines: int = 0
    rate_hz: float = 0.0
    device_message: str = "Join the Arduino Wi-Fi network before connecting."
    active_range_g: int | None = None
    range_control: bool = False
    pending_range_g: int | None = None
    recording: bool = False
    recording_pending: bool = False
    recording_path: Path | None = None
    recorded_samples: int = 0


@dataclass(frozen=True)
class PlotSnapshot:
    generation: int
    points: tuple[tuple[int, Sample], ...]


class SensorClient:
    """Keep all network and disk I/O outside Tk's event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._plot_lock = threading.Lock()
        self._plot_history: deque[tuple[int, Sample]] = deque(maxlen=MAX_PLOT_SAMPLES)
        self._plot_start_ms: int | None = None
        self._plot_generation = 0
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._arrival_times: deque[float] = deque()
        self._last_valid_sample = 0.0
        self._range_deadline: float | None = None
        self._file: TextIO | None = None
        self._writer = None
        self._last_flush = 0.0
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="KX134 acquisition")
        self._thread.start()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def _update(self, **changes) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def plot_snapshot(self) -> PlotSnapshot:
        """Copy a bounded display history; CSV recording never reads this buffer."""
        with self._plot_lock:
            return PlotSnapshot(self._plot_generation, tuple(self._plot_history))

    def _reset_plot(self) -> None:
        with self._plot_lock:
            self._plot_history.clear()
            self._plot_start_ms = None
            self._plot_generation += 1

    def _add_plot_sample(self, sample: Sample) -> None:
        with self._plot_lock:
            if self._plot_start_ms is None:
                self._plot_start_ms = sample.read_ms
            # Arduino millis() is unsigned 32-bit; preserve elapsed time at wrap.
            elapsed_ms = (sample.read_ms - self._plot_start_ms) & 0xFFFFFFFF
            self._plot_history.append((elapsed_ms, sample))
            while self._plot_history[0][0] < elapsed_ms - PLOT_WINDOW_MS:
                self._plot_history.popleft()

    def connect(self, host: str, port: int) -> None:
        if self.snapshot().phase != "disconnected":
            return
        self._reset_plot()
        self._update(phase="connecting", status=f"Connecting to {host}:{port}…",
                     latest=None, received_samples=0, invalid_lines=0, rate_hz=0.0,
                     device_message="Waiting for the Arduino…")
        self._commands.put(("connect", (host, port)))

    def disconnect(self) -> None:
        self._update(phase="disconnecting", status="Disconnecting…")
        self._commands.put(("disconnect", None))

    def start_recording(self, folder: Path, filename: str = "") -> None:
        self._update(recording_pending=True)
        self._commands.put(("start", (folder, filename)))

    def set_range(self, range_g: int) -> None:
        state = self.snapshot()
        if (range_g not in SENSOR_RANGES or state.phase != "connected"
                or not state.range_control or state.pending_range_g is not None
                or state.recording or state.recording_pending or range_g == state.active_range_g):
            return
        self._update(pending_range_g=range_g)
        self._commands.put(("range", range_g))

    def stop_recording(self) -> None:
        self._update(recording_pending=True)
        self._commands.put(("stop", None))

    def close(self) -> None:
        """Request an orderly close; the GUI waits without blocking its event loop."""
        self._commands.put(("close", None))

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _open_recording(self, folder: Path, requested_name: str) -> None:
        if self._file is not None:
            self._update(recording_pending=False)
            return
        state = self.snapshot()
        if state.pending_range_g is not None:
            self._update(recording_pending=False)
            self.events.put(("error", "Wait for the Arduino to confirm the range before recording."))
            return
        if self._socket is None or state.latest is None:
            self._update(recording_pending=False)
            self.events.put(("error", "Connect and receive sensor data before recording."))
            return
        filename = requested_name.strip()
        if filename:
            has_extension = filename.lower().endswith(".csv")
            stem = filename[:-4] if has_extension else filename
            if stem.strip() in {"", ".", ".."} or any(
                ch in "/\\:" or ord(ch) < 32 or ord(ch) == 127 for ch in filename
            ):
                self._update(recording_pending=False)
                self.events.put(("error", "Enter a file name without a path. "
                                 "Use Choose folder to select where it goes."))
                return
            if not has_extension:
                filename += ".csv"
        else:
            filename = datetime.now().strftime("kx134_%Y%m%d_%H%M%S_%f.csv")

        path = folder / filename
        try:
            # Exclusive creation prevents an existing trial from being overwritten.
            self._file = path.open("x", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(CSV_COLUMNS)
            self._file.flush()
            self._last_flush = time.monotonic()
        except FileExistsError:
            self._update(recording_pending=False)
            self.events.put(("error", f"{filename} already exists in this folder. "
                             "Choose a different file name or leave it blank."))
            return
        except OSError as exc:
            self._finish_recording()
            self._update(recording_pending=False)
            self.events.put(("error", f"Could not create the CSV file:\n{exc}"))
            return
        self._update(recording=True, recording_pending=False,
                     recording_path=path, recorded_samples=0)
        self._reset_plot()  # The first sample in this CSV is plotted at 0 ms.

    def _finish_recording(self) -> None:
        """Close the current trial on Stop, disconnect, timeout, or window close."""
        try:
            if self._file is not None:
                self._file.close()  # close() also flushes buffered CSV rows.
        except OSError as exc:
            self.events.put(("error", f"Could not finish writing the CSV:\n{exc}"))
        finally:
            self._file = None
            self._writer = None
            self._update(recording=False, recording_pending=False)

    def _disconnect(self, status: str = "Disconnected") -> None:
        self._finish_recording()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._buffer.clear()
        self._range_deadline = None
        self._update(phase="disconnected", status=status, rate_hz=0.0,
                     active_range_g=None, pending_range_g=None, range_control=False)

    def _range_unconfirmed(self, message: str) -> None:
        # A command might have reached the board even if its reply never did.
        # Reconnect to get the actual range before writing another recording.
        self._disconnect("Range not confirmed; reconnect to the Arduino")
        self.events.put(("error", message + " Reconnect to check the active range."))

    def _handle_command(self, command: str, argument: object) -> None:
        if command == "connect":
            host, port = argument
            try:
                self._socket = socket.create_connection((host, port), CONNECT_TIMEOUT)
                self._socket.settimeout(RECEIVE_TIMEOUT)
                self._buffer.clear()
                self._arrival_times.clear()
                self._last_valid_sample = time.monotonic()
                self._update(phase="connected", status="Connected — waiting for data")
            except OSError as exc:
                self._disconnect("Connection failed")
                self.events.put(("error", f"Could not connect to {host}:{port}.\n\n"
                                 "Join the Arduino Wi-Fi network and close any nc "
                                 f"session using the sensor.\n\n{exc}"))
        elif command == "disconnect":
            self._disconnect()
        elif command == "start":
            folder, filename = argument
            self._open_recording(folder, filename)
        elif command == "range":
            if self._socket is not None and self.snapshot().pending_range_g == argument:
                try:
                    self._socket.sendall(f"RANGE {argument}\n".encode("ascii"))
                    self._range_deadline = time.monotonic() + RANGE_TIMEOUT
                except OSError as exc:
                    self._range_unconfirmed(f"Could not send the range request: {exc}.")
        elif command == "stop":
            self._finish_recording()
        elif command == "close":
            self._disconnect()
            self._running = False

    def _consume_comment(self, line: str) -> None:
        state = self.snapshot()
        report = RANGE_REPORT.fullmatch(line)
        if report:
            range_g = int(report[1])
            if state.pending_range_g is not None and range_g != state.pending_range_g:
                self._range_unconfirmed("The Arduino reported an unexpected range.")
                return
            self._range_deadline = None
            if range_g != state.active_range_g:
                if self._file is not None:
                    self._finish_recording()
                    self.events.put(("error", "The sensor range changed. The recording was stopped."))
                self._reset_plot()
                self._update(latest=None, status="Connected — waiting for data")
            self._update(active_range_g=range_g, pending_range_g=None, range_control=True)
        elif line.startswith("# RANGE ERROR "):
            self._range_deadline = None
            self._update(pending_range_g=None)
            self.events.put(("error", line.removeprefix("# RANGE ERROR ")))
        else:
            legacy = LEGACY_RANGE.match(line)
            if not state.range_control and legacy and int(legacy[1]) in SENSOR_RANGES:
                self._update(active_range_g=int(legacy[1]))
            self._update(device_message=line.lstrip("# "))

    def _consume_line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8").strip()
            sample = parse_sample(line)
        except (UnicodeError, ValueError, csv.Error):
            state = self.snapshot()
            self._update(invalid_lines=state.invalid_lines + 1,
                         device_message="Skipped a malformed sensor row.")
            return
        if sample is None:
            if line.startswith("#"):
                self._consume_comment(line)
            return

        self._last_valid_sample = sample.received_monotonic
        self._arrival_times.append(sample.received_monotonic)
        while self._arrival_times[0] < sample.received_monotonic - 2.0:
            self._arrival_times.popleft()
        elapsed = self._arrival_times[-1] - self._arrival_times[0]
        rate = (len(self._arrival_times) - 1) / elapsed if elapsed > 0 else 0.0
        state = self.snapshot()
        self._update(latest=sample, received_samples=state.received_samples + 1,
                     rate_hz=rate, status="Connected — receiving data")

        if self._writer is not None:
            try:
                self._writer.writerow(sample.csv_row(state.active_range_g))
                self._update(recorded_samples=state.recorded_samples + 1)
            except OSError as exc:
                self._finish_recording()
                self.events.put(("error", f"Recording stopped because writing failed:\n{exc}"))

        # Append only after the recording write. Display history may be pruned;
        # the full recording is independent of plotting and its refresh rate.
        self._add_plot_sample(sample)

    def _receive(self) -> None:
        try:
            data = self._socket.recv(4096)
            if not data:
                self._disconnect("Arduino closed the connection; recording stopped")
                return
            self._buffer.extend(data)
            # TCP is a byte stream: a receive can contain partial or multiple rows.
            while b"\n" in self._buffer:
                raw, _, remainder = self._buffer.partition(b"\n")
                self._buffer = bytearray(remainder)
                if len(raw) > MAX_LINE_BYTES:
                    raise ValueError("Sensor sent an unexpectedly long line")
                self._consume_line(raw)
                if self._socket is None:
                    return
            if len(self._buffer) > MAX_LINE_BYTES:
                raise ValueError("Sensor sent an unexpectedly long line")
        except socket.timeout:
            pass
        except (OSError, ValueError) as exc:
            self._disconnect("Connection lost; recording stopped")
            self.events.put(("error", f"Sensor connection ended:\n{exc}"))
            return

        now = time.monotonic()
        if self._range_deadline is not None and now >= self._range_deadline:
            self._range_unconfirmed("The Arduino did not confirm the change in time.")
        elif now - self._last_valid_sample > NO_DATA_TIMEOUT:
            self._disconnect("No sensor data for 5 seconds; recording stopped")
            self.events.put(("error", "No valid sensor data arrived for 5 seconds. "
                             "Check Arduino power, Wi-Fi, and any open nc session."))
        elif self._file is not None and now - self._last_flush >= 1.0:
            try:
                self._file.flush()
                self._last_flush = now
            except OSError as exc:
                self._finish_recording()
                self.events.put(("error", f"Recording stopped because writing failed:\n{exc}"))

    def _run(self) -> None:
        try:
            while self._running:
                try:
                    command, argument = self._commands.get(
                        timeout=0 if self._socket is not None else RECEIVE_TIMEOUT)
                except queue.Empty:
                    pass
                else:
                    self._handle_command(command, argument)
                    continue
                if self._socket is not None:
                    self._receive()
        finally:
            self._disconnect()
