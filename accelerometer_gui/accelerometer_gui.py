#!/usr/bin/env python3
"""Live KX134 readings and local CSV recording for the UNO R4 WiFi test.

Launch with Launch Accelerometer GUI.command, or a Python with Tkinter installed.
Requires Tkinter and Matplotlib. Start/Stop controls laptop recording.
Apply Range changes the KX134 setting on the Arduino between trials.
"""

from __future__ import annotations

from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from sensor_client import PLOT_WINDOW_MS, SENSOR_RANGES, SensorClient


DISPLAY_REFRESH_MS = 100
RANGE_CHOICES = {f"±{value} g": value for value in SENSOR_RANGES}


class AccelerometerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("KX134 Accelerometer")
        self.root.geometry("1120x820")
        self.root.minsize(1040, 760)
        self.client = SensorClient()
        self.folder: Path | None = None
        self.closing = False
        self._plot_key = None

        self.host = tk.StringVar(value="192.48.56.2")
        self.port = tk.StringVar(value="5000")
        self.status = tk.StringVar(value="Disconnected")
        self.range_choice = tk.StringVar()
        self.range_info = tk.StringVar(value="Connect to see the active range.")
        self._shown_range = None
        self.axes = [tk.StringVar(value="—") for _ in range(3)]
        self.sample_info = tk.StringVar(value="Waiting for sensor data")
        self.device_info = tk.StringVar(value="Join the Arduino Wi-Fi network before connecting.")
        self.folder_text = tk.StringVar(value="Choose a folder for your recordings")
        self.filename = tk.StringVar()
        self.recording_info = tk.StringVar(value="Not recording")
        self.file_text = tk.StringVar(value="Each recording creates a new CSV file.")

        self._build_window()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(DISPLAY_REFRESH_MS, self._refresh)

    def _build_window(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        body = ttk.Frame(self.root, padding=16)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(6, weight=1)

        ttk.Label(body, text="KX134 Accelerometer", font=("TkDefaultFont", 20, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(body, text="UNO R4 WiFi · Live plots and CSV recording").grid(
            row=1, column=0, sticky="w", pady=(4, 12))

        connection = ttk.LabelFrame(body, text="Connection", padding=12)
        connection.grid(row=2, column=0, sticky="ew")
        ttk.Label(connection, text="Address").grid(row=0, column=0, padx=(0, 6))
        self.host_entry = ttk.Entry(connection, textvariable=self.host, width=18)
        self.host_entry.grid(row=0, column=1, padx=(0, 14))
        ttk.Label(connection, text="Port").grid(row=0, column=2, padx=(0, 6))
        self.port_entry = ttk.Entry(connection, textvariable=self.port, width=7)
        self.port_entry.grid(row=0, column=3, padx=(0, 14))
        self.connect_button = ttk.Button(connection, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=4, padx=(0, 6))
        self.disconnect_button = ttk.Button(connection, text="Disconnect",
                                            command=self.client.disconnect, state="disabled")
        self.disconnect_button.grid(row=0, column=5)
        ttk.Label(connection, textvariable=self.status).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))

        range_controls = ttk.Frame(connection)
        range_controls.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Label(range_controls, text="Sensor range").pack(side="left", padx=(0, 6))
        self.range_menu = ttk.Combobox(range_controls, textvariable=self.range_choice,
                                       values=tuple(RANGE_CHOICES), width=8,
                                       state="readonly")
        self.range_menu.state(["disabled"])
        self.range_menu.pack(side="left", padx=(0, 8))
        self.range_menu.bind("<<ComboboxSelected>>", lambda _event: self._update_controls())
        self.range_button = ttk.Button(range_controls, text="Apply Range",
                                       command=self._apply_range, state="disabled")
        self.range_button.pack(side="left", padx=(0, 10))
        ttk.Label(range_controls, textvariable=self.range_info).pack(side="left")

        readings = ttk.Frame(body)
        readings.grid(row=3, column=0, sticky="ew", pady=(12, 8))
        for index, name in enumerate(("X", "Y", "Z")):
            readings.columnconfigure(index, weight=1, uniform="axis")
            box = ttk.LabelFrame(readings, text=f"{name} acceleration (g)", padding=8)
            box.grid(row=0, column=index, sticky="ew", padx=(0, 12) if index < 2 else 0)
            ttk.Label(box, textvariable=self.axes[index], font=("TkDefaultFont", 22, "bold"),
                      anchor="center").pack(fill="x")
        ttk.Label(body, textvariable=self.sample_info).grid(row=4, column=0, sticky="w")
        ttk.Label(body, textvariable=self.device_info, wraplength=1020).grid(
            row=5, column=0, sticky="w", pady=(5, 10))

        content = ttk.Frame(body)
        content.grid(row=6, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        plot_panel = ttk.LabelFrame(content, text="Live acceleration · last 10 seconds", padding=8)
        plot_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        plot_panel.rowconfigure(0, weight=1)
        plot_panel.columnconfigure(0, weight=1)
        self.figure = Figure(figsize=(6.6, 4.2), dpi=100)
        self.figure.subplots_adjust(left=0.105, right=0.94, bottom=0.13, top=0.97, hspace=0.22)
        self.plot_axes = self.figure.subplots(3, 1, sharex=True)
        self.plot_lines = []
        for ax, name in zip(self.plot_axes, ("X", "Y", "Z")):
            line, = ax.plot([], [], linewidth=1.0)
            self.plot_lines.append(line)
            ax.set_ylabel(f"{name} (g)")
            ax.grid(True, alpha=0.25)
            ax.set_xlim(0, PLOT_WINDOW_MS)
            ax.tick_params(labelsize=9)
        self.plot_axes[-1].set_xlabel("Elapsed time (ms)")
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_panel)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self.canvas.draw_idle()

        recording = ttk.LabelFrame(content, text="Recording", padding=12)
        recording.grid(row=0, column=1, sticky="nsew")
        recording.columnconfigure(0, weight=1)
        self.folder_button = ttk.Button(recording, text="Choose folder…", command=self._choose_folder)
        self.folder_button.grid(row=0, column=0, sticky="ew")
        ttk.Label(recording, textvariable=self.folder_text, wraplength=280).grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(recording, text="File name (optional)").grid(
            row=2, column=0, sticky="w", pady=(12, 3))
        self.filename_entry = ttk.Entry(recording, textvariable=self.filename)
        self.filename_entry.grid(row=3, column=0, sticky="ew")
        ttk.Label(recording, text="Leave blank to use a timestamp.").grid(
            row=4, column=0, sticky="w", pady=(4, 0))
        actions = ttk.Frame(recording)
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 8))
        self.start_button = ttk.Button(actions, text="Start recording", command=self._start_recording,
                                      state="disabled")
        self.start_button.pack(fill="x", pady=(0, 6))
        self.stop_button = ttk.Button(actions, text="Stop recording", command=self.client.stop_recording,
                                     state="disabled")
        self.stop_button.pack(fill="x")
        ttk.Label(recording, textvariable=self.recording_info, wraplength=280).grid(
            row=6, column=0, sticky="w")
        ttk.Label(recording, textvariable=self.file_text, wraplength=280).grid(
            row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Label(recording, text="Plot time starts at zero on connection and at the "
                  "start of each recording. The view scrolls after 10 seconds.",
                  wraplength=280).grid(row=8, column=0, sticky="w", pady=(12, 0))
        ttk.Label(body, text="Live readings continue when recording is stopped.").grid(
            row=7, column=0, sticky="w", pady=(12, 0))

    def _connect(self) -> None:
        host = self.host.get().strip()
        try:
            port = int(self.port.get())
            if not host or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Connection settings", "Enter an address and a port from 1 to 65535.",
                                 parent=self.root)
            return
        self.client.connect(host, port)
        self._update_controls()

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose a recording folder", mustexist=True,
                                           initialdir=str(self.folder or Path.home()), parent=self.root)
        if selected:
            self.folder = Path(selected)
            self.folder_text.set(str(self.folder))
            self._update_controls()

    def _start_recording(self) -> None:
        if self.folder is not None:
            self.client.start_recording(self.folder, self.filename.get())
            self._update_controls()

    def _apply_range(self) -> None:
        selected = RANGE_CHOICES.get(self.range_choice.get())
        if selected is not None:
            self.client.set_range(selected)
            self._update_controls()

    @staticmethod
    def _set_enabled(widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _update_controls(self) -> None:
        state = self.client.snapshot()
        idle = state.phase == "disconnected" and not self.closing
        connected = state.phase == "connected" and not self.closing
        self._set_enabled(self.connect_button, idle)
        self._set_enabled(self.disconnect_button, connected)
        self._set_enabled(self.host_entry, idle)
        self._set_enabled(self.port_entry, idle)
        if self._shown_range != state.active_range_g:
            self._shown_range = state.active_range_g
            self.range_choice.set(f"±{self._shown_range} g" if self._shown_range else "")
        selected = RANGE_CHOICES.get(self.range_choice.get())
        unapplied = state.range_control and selected is not None and selected != state.active_range_g
        can_change = (connected and state.range_control and state.pending_range_g is None
                      and not state.recording and not state.recording_pending)
        self._set_enabled(self.range_menu, can_change)
        self._set_enabled(self.range_button, can_change and unapplied)
        active = f"Active: ±{state.active_range_g} g" if state.active_range_g else "Range not reported"
        if state.pending_range_g is not None:
            self.range_info.set(f"{active} · Applying ±{state.pending_range_g} g…")
        elif not connected:
            self.range_info.set("Connect to see the active range.")
        elif state.recording and state.range_control:
            self.range_info.set(f"{active} · Locked during recording")
        elif not state.range_control:
            self.range_info.set(f"{active} · Updated firmware required to change range")
        elif unapplied:
            self.range_info.set(f"{active} · Click Apply Range to use ±{selected} g")
        else:
            self.range_info.set(active)
        can_edit_destination = not state.recording and not state.recording_pending and not self.closing
        self._set_enabled(self.folder_button, can_edit_destination)
        self._set_enabled(self.filename_entry, can_edit_destination)
        fresh = state.latest is not None and time.monotonic() - state.latest.received_monotonic < 2
        self._set_enabled(self.start_button, connected and fresh and self.folder is not None
                          and not state.recording and not state.recording_pending
                          and state.pending_range_g is None and not unapplied)
        self._set_enabled(self.stop_button, state.recording and not state.recording_pending
                          and not self.closing)

    def _refresh(self) -> None:
        state = self.client.snapshot()
        self.status.set("Finishing recording and closing…" if self.closing else state.status)
        self.device_info.set(state.device_message)
        if state.latest is not None:
            for variable, value in zip(self.axes, (state.latest.x_g, state.latest.y_g, state.latest.z_g)):
                variable.set(f"{value:+.4f}")
            age = time.monotonic() - state.latest.received_monotonic
            self.sample_info.set(
                f"Received: {state.received_samples:,}   ·   {state.rate_hz:.1f} samples/s   ·   "
                f"Last sample: {age:.1f} s ago   ·   Invalid rows: {state.invalid_lines}")
        else:
            for variable in self.axes:
                variable.set("—")
            self.sample_info.set("Waiting for sensor data")

        if state.recording:
            self.recording_info.set(f"Recording · {state.recorded_samples:,} samples saved")
        elif state.recording_path is not None:
            self.recording_info.set(f"Recording stopped · {state.recorded_samples:,} samples saved")
        else:
            self.recording_info.set("Not recording")
        if state.recording_path is not None:
            self.file_text.set(f"CSV: {state.recording_path}")
        self._update_controls()
        self._refresh_plot()

        # The worker never calls Tk directly. All dialogs and widget updates run here.
        while True:
            try:
                kind, message = self.client.events.get_nowait()
            except queue.Empty:
                break
            if kind == "error" and not self.closing:
                messagebox.showerror("Accelerometer", message, parent=self.root)
        if self.closing and not self.client.is_alive():
            self.root.destroy()
            return
        self.root.after(DISPLAY_REFRESH_MS, self._refresh)

    def _refresh_plot(self) -> None:
        """Reuse three line artists; do not redraw once for every sensor sample."""
        history = self.client.plot_snapshot()
        points = history.points
        key = (history.generation, points[-1][1].received_monotonic if points else None)
        if key == self._plot_key:
            return
        self._plot_key = key
        times = [elapsed_ms for elapsed_ms, sample in points]
        for ax, line, field in zip(self.plot_axes, self.plot_lines, ("x_g", "y_g", "z_g")):
            line.set_data(times, [getattr(sample, field) for elapsed_ms, sample in points])
            ax.relim()
            ax.autoscale_view(scalex=False, scaley=True)
        right = max(PLOT_WINDOW_MS, times[-1] if times else PLOT_WINDOW_MS)
        self.plot_axes[-1].set_xlim(right - PLOT_WINDOW_MS, right)
        # Tk merges pending draws and performs rendering on the GUI thread.
        self.canvas.draw_idle()

    def _close(self) -> None:
        if not self.closing:
            self.closing = True
            self.client.close()
            self._update_controls()


def main() -> None:
    root = tk.Tk()
    AccelerometerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
