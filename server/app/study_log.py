"""Load the same stdlib-only study logger shipped with the Slicer/SSH client."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_source = next(
    root / "LiveSegmentation" / "LiveSegmentationLib" / "work_log.py"
    for root in (Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[1])
    if (root / "LiveSegmentation" / "LiveSegmentationLib" / "work_log.py").is_file()
)
_spec = importlib.util.spec_from_file_location("liveseg_study_log", _source)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
RoomWorkLog = _module.RoomWorkLog
operation_details = _module.operation_details
