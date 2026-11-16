"""Plot a KX134 CSV as three stacked axes; default to the newest recording."""

import csv
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def is_recording(path):
    if not path.is_file() or path.suffix.lower() != ".csv":
        return False
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            return {"read_ms", "x_g", "y_g", "z_g"}.issubset(next(csv.reader(stream), ()))
    except (OSError, UnicodeError, csv.Error):
        return False


data_folder = Path(__file__).resolve().parents[1] / "data"
csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(
    filter(is_recording, data_folder.iterdir()), key=lambda path: path.stat().st_mtime
)
with csv_path.open(newline="", encoding="utf-8-sig") as stream:
    rows = list(csv.DictReader(stream))

start_ms = int(rows[0]["read_ms"])
time_ms = [int(row["read_ms"]) - start_ms for row in rows]

fig, axes = plt.subplots(3, 1, sharex=True, figsize=(11, 7))
for ax, name in zip(axes, ("x", "y", "z")):
    ax.plot(time_ms, [float(row[f"{name}_g"]) for row in rows], linewidth=0.8)
    ax.set_ylabel(f"{name.upper()} (g)")
    ax.grid(True, alpha=0.25)

axes[-1].set_xlabel("Time (ms)")
axes[-1].set_xlim(0, time_ms[-1])
fig.suptitle("KX134 accelerometer recording")
fig.tight_layout()
output = csv_path.with_suffix(".png")
fig.savefig(output, dpi=200)
plt.close(fig)
print(output.resolve())
