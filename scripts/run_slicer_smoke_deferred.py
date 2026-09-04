"""Run the GUI-dependent Slicer smoke probe after the main window exists."""

from pathlib import Path

import qt


def run_probe_script():
    script_path = Path(__file__).with_name("slicer_live_segmentation_smoke_test.py")
    source = script_path.read_text(encoding="utf-8")
    exec(compile(source, str(script_path), "exec"), {"__name__": "__main__"})


qt.QTimer.singleShot(1000, run_probe_script)
