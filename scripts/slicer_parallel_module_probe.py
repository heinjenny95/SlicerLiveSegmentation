from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import slicer


def module_details(name):
    module = slicer.app.moduleManager().module(name)
    if module is None:
        return None
    representation = module.widgetRepresentation()
    widget = representation.self() if representation is not None else None
    return {
        "name": name,
        "path": slicer.util.modulePath(name),
        "title": getattr(module, "title", ""),
        "widget_class": type(widget).__name__ if widget is not None else None,
        "has_live_collaboration": hasattr(widget, "live_collaboration"),
    }


def run_probe():
    try:
        companion_name = os.environ.get("LIVE_SEGMENTATION_COMPANION_MODULE", "").strip()
        if not companion_name:
            raise RuntimeError("LIVE_SEGMENTATION_COMPANION_MODULE is required")
        companion = module_details(companion_name)
        standalone = module_details("LiveSegmentation")
        if companion is None:
            raise RuntimeError(f"Companion module is not registered: {companion_name}")
        if standalone is None:
            raise RuntimeError("LiveSegmentation module is not registered")
        if Path(companion["path"]).resolve() == Path(standalone["path"]).resolve():
            raise RuntimeError("Companion and LiveSegmentation resolve to the same file")
        if standalone["widget_class"] != "LiveSegmentationWidget":
            raise RuntimeError(f"Unexpected standalone widget: {standalone['widget_class']}")
        return {"ok": True, "companion": companion, "standalone": standalone}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}


output_path = os.environ.get("LIVE_SEGMENTATION_PARALLEL_PROBE_OUTPUT")
if not output_path:
    raise RuntimeError("LIVE_SEGMENTATION_PARALLEL_PROBE_OUTPUT is required")

result = run_probe()
Path(output_path).write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
slicer.app.exit(0 if result["ok"] else 1)
