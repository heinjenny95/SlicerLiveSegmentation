"""Smoke-test that the Live Segmentation module remains usable in a narrow panel."""

from __future__ import annotations

import json
import os
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
        controller = scripted_widget.live_collaboration
        compact = {
            "advanced_collapsed": bool(controller.advanced_group.collapsed),
            "follow_controls_collapsed": bool(controller.collaborators_group.collapsed),
            "permissions_collapsed": bool(controller.permissions_group.collapsed),
            "empty_preflight_hidden": not bool(controller.preflight_text.visible),
            "chat_outside_advanced": controller.open_chat_dock_button.parent() == controller.group,
            "label_controls_outside_advanced": controller.labels_group.parent() == controller.group,
            "two_lock_modes": controller.lock_mode_combo.count == 2,
        }
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
        modes = []
        for index in range(4):
            controller.transport_combo.setCurrentIndex(index)
            slicer.app.processEvents()
            modes.append({
                "mode": controller._transport_mode(),
                "minimum_height": int(_value(root.minimumSizeHint, "height")),
                "https_fields": bool(controller.server_settings.visible),
                "lan_fields": bool(controller.lan_settings.visible),
                "ssh_fields": bool(controller.ssh_settings.visible),
            })
        controller.transport_combo.setCurrentIndex(0)
        slicer.app.processEvents()
        result = {
            "ok": root_minimum_hint <= 360 and all(compact.values())
            and all(mode["minimum_height"] <= 850 for mode in modes)
            and [mode["https_fields"] for mode in modes] == [False, False, True, False]
            and [mode["lan_fields"] for mode in modes] == [False, True, False, False]
            and [mode["ssh_fields"] for mode in modes] == [False, False, False, True],
            "compact_layout": compact,
            "transport_layouts": modes,
            "root_minimum_hint": root_minimum_hint,
            "root_size_hint": _width(root.sizeHint),
            "widest_children": widest[:15],
        }
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        output = Path(os.environ.get("LIVESEG_UI_PROBE_OUTPUT", str(Path(__file__).with_suffix(".result.json"))))
        output.write_text(serialized, encoding="utf-8")
        if os.environ.get("LIVESEG_UI_PROBE_SCREENSHOT"):
            root.grab().save(os.environ["LIVESEG_UI_PROBE_SCREENSHOT"])
        print("LIVESEG_UI_WIDTH_RESULT=" + serialized, flush=True)
        slicer.app.exit(0 if result["ok"] else 2)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        Path(os.environ.get("LIVESEG_UI_PROBE_OUTPUT", str(Path(__file__).with_suffix(".result.json")))).write_text(serialized, encoding="utf-8")
        print("LIVESEG_UI_WIDTH_RESULT=" + serialized, flush=True)
        slicer.app.exit(1)


qt.QTimer.singleShot(1200, run)
