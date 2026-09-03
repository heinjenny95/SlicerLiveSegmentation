"""Smoke-test that the Live Segmentation module remains usable in a narrow panel."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import qt
import slicer


def _value(obj, name, default=None):
    value = getattr(obj, name, default)
    return value() if callable(value) else value


def _width(size):
    return int(_value(size, "width", 0) or 0)


def run():
    try:
        slicer.util.selectModule("LiveSegmentation")
        slicer.app.processEvents()
        representation = slicer.modules.livesegmentation.widgetRepresentation()
        scripted_widget = representation.self()
        root = scripted_widget.parent
        descendants = list(root.findChildren(qt.QWidget))
        widest = sorted(
            (
                {
                    "class": widget.className(),
                    "name": str(_value(widget, "objectName", "")),
                    "text": str(_value(widget, "text", ""))[:100],
                    "minimum_hint": _width(widget.minimumSizeHint),
                    "size_hint": _width(widget.sizeHint),
                }
                for widget in descendants
            ),
            key=lambda item: item["minimum_hint"],
            reverse=True,
        )
        root_minimum_hint = _width(root.minimumSizeHint)
        result = {
            "ok": root_minimum_hint <= 360,
            "root_minimum_hint": root_minimum_hint,
            "root_size_hint": _width(root.sizeHint),
            "widest_children": widest[:15],
        }
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        Path(__file__).with_suffix(".result.json").write_text(serialized, encoding="utf-8")
        print("LIVESEG_UI_WIDTH_RESULT=" + serialized, flush=True)
        slicer.app.exit(0 if result["ok"] else 2)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        Path(__file__).with_suffix(".result.json").write_text(serialized, encoding="utf-8")
        print("LIVESEG_UI_WIDTH_RESULT=" + serialized, flush=True)
        slicer.app.exit(1)


qt.QTimer.singleShot(1200, run)
