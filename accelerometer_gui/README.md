# KX134 Accelerometer GUI

A small Tkinter application for the UNO R4 WiFi TCP readout sketch.
It displays the latest X/Y/Z acceleration in **g**, shows three scrolling live
plots and the received sample rate, and records samples to CSV in a folder you
choose. Plots remain live both during and between recordings.

## Launch on this Mac

1. Upload the updated `wifi-sensor-test/wifi-sensor-test.ino` sketch once using
   Arduino IDE. Power the board and join its Wi-Fi access point from the laptop.
2. Close any `nc` session with **Control-C**. The Arduino
   sketch serves one TCP client at a time.
3. Double-click **Launch Accelerometer GUI.command**.
4. Click **Connect**. The default endpoint is `192.48.56.2:5000`.
5. Under **Sensor range**, select **±8 g**, **±16 g**, **±32 g**, or **±64 g**.
   Click **Apply Range** and wait for the **Active** label to show the new range.
6. Click **Choose folder…**, optionally enter a **File name**, then click **Start recording**.
7. Click **Stop recording** to finish the CSV. Live readings continue.

The launcher uses `.venv` in this folder, created from the installed Python 3.12
with Tkinter and Matplotlib. The current default Python 3.14 lacks `_tkinter`.
To launch directly from Terminal:

```bash
cd "/Users/johngray/Documents/University of Memphis/Fall 2026/Bioinstrumentation-BIOM8209/Project/Code/accelerometer_gui"
./.venv/bin/python accelerometer_gui.py
```

To recreate the environment if needed, run these commands from this folder:

```bash
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

## Live plots and latency

- Three stacked panels show X, Y, and Z in g, using the Arduino's `read_ms`
  timestamps rather than Wi-Fi arrival times.
- Elapsed time begins at 0 ms after connecting and resets after a range change
  and at the first sample of each new recording. The view scrolls after 10,000 ms.
- Plotting reuses three line objects and refreshes at most ten times per second.
  Samples received between redraws remain in the plotted history and CSV.
- Plot history is limited to ten seconds and a maximum of 5,000 samples. CSV
  recordings are not limited by that display buffer.
- A 100 ms refresh interval adds up to roughly 100 ms of display waiting,
  plus rendering and network delays. This is not a measured end-to-end latency.
  A busy laptop may refresh more slowly. CSV writing runs in the acquisition
  worker rather than in plot callbacks; heavy CPU load can still affect timing.
- All Matplotlib/Tk drawing stays on the GUI thread, using `draw_idle()` to merge
  redundant redraw requests. There is no redraw for each individual sample.
- To reduce display CPU use, increase `DISPLAY_REFRESH_MS` in
  `accelerometer_gui.py`, for example from 100 to 200 for at most five redraws/s.

## Recording behavior

- Enter a name such as `trial-01` to save `trial-01.csv` in the chosen folder.
  You can include `.csv` or let the app add it. A blank name uses a timestamped
  file such as `kx134_20261108_153000_123456.csv`. If the name is already taken,
  the app asks for a different name. Existing recordings are never overwritten.
- Start/Stop controls **laptop file recording**. The Arduino continuously streams
  while connected. Change the range between recordings so a trial uses one range.
- The Arduino starts at **±64 g** after a power cycle or reset. The range stays
  selected when the GUI disconnects and reconnects without restarting the board.
- If a change is not confirmed, the GUI disconnects so it can learn the current
  range on reconnect. Older firmware can still stream and record, but cannot
  change range from the GUI.
- Recording boundaries take effect when the acquisition worker processes the
  button request, normally within the 0.1-second socket polling interval.
- The background worker saves every valid sample it receives during a recording,
  including samples between GUI refreshes. CSV data is flushed about once per
  second and the file is closed on Stop, disconnect, or normal window close.
- Connection loss or five seconds without valid samples ends the connection
  and closes the recording. Reconnect and start a new recording to continue.
- Live values remain visible as the last received readings after disconnect;
  the connection status and sample age distinguish them from current data.
- The Arduino's comments and column header are skipped. Malformed/non-finite
  rows are counted and excluded from recordings.

CSV columns:

| Column | Meaning |
| --- | --- |
| `received_at_utc` | Laptop receipt time in UTC, including the UTC offset |
| `read_ms` | Arduino uptime in milliseconds when the sensor was read |
| `x_g`, `y_g`, `z_g` | Acceleration along each sensor axis, in g |
| `range_g` | Full-scale setting, e.g. `32` means ±32 g. Blank if the board did not report a range. |

The displayed rate is based on recent **laptop arrivals**, not a measurement of
the sensor's configured output rate. Wi-Fi buffering can make arrivals uneven.
The current Arduino firmware has no sample sequence numbers or FIFO loss
counter, so this app cannot detect samples missed before transmission.

## Files

- `accelerometer_gui.py`: Tkinter interface.
- `sensor_client.py`: TCP stream parsing and CSV recording worker.
- `Launch Accelerometer GUI.command`: macOS launcher.
- `requirements.txt`: Matplotlib dependency for the live plots.
- `test_sensor_client.py`: local TCP recording, reconnect, and range checks. Run with
  `python3 -m unittest -v` from this folder using a Python with Tkinter available
  if you also want to launch the interface (the worker tests do not require Tk).

