#!/bin/zsh
# Prefer the project environment containing Tkinter and Matplotlib.
cd -- "${0:A:h}" || exit 1
export MPLCONFIGDIR="${PWD}/.cache/matplotlib"
export XDG_CACHE_HOME="${PWD}/.cache"
mkdir -p -- "$MPLCONFIGDIR"
for gui_python in .venv/bin/python /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 python3; do
  if "$gui_python" -c 'import tkinter; from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg' >/dev/null 2>&1; then
    exec "$gui_python" accelerometer_gui.py
  fi
done
print 'This app needs Python with Tkinter and Matplotlib. From this folder, run:'
print '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv .venv'
print './.venv/bin/python -m pip install -r requirements.txt'
read '?Press Return to close this window.'
