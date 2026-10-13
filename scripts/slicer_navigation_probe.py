"""Verify a module-independent return action without changing the scene."""
import json
import os
import traceback
from pathlib import Path

import qt
import slicer

_WIDGET_REFERENCE = None


def run():
    global _WIDGET_REFERENCE
    result = {"ok": False}
    try:
        slicer.util.selectModule("LiveSegmentation")
        widget = slicer.modules.livesegmentation.widgetRepresentation().self()
        _WIDGET_REFERENCE = widget
        assert getattr(widget, "navigation_toolbar", None) is not None, "No global collaboration navigation"
        before = slicer.mrmlScene.GetNumberOfNodes()
        for _ in range(3):
            widget._setup_navigation_toolbar()
        bars = [bar for bar in slicer.util.mainWindow().findChildren(qt.QToolBar)
                if str(bar.objectName) == "LiveSegmentationNavigation"]
        assert len(bars) == 1, "Duplicate global toolbar"
        slicer.util.selectModule("Data")
        assert slicer.util.selectedModule() == "Data"
        widget.navigation_action.trigger()
        assert slicer.util.selectedModule() == "LiveSegmentation"
        assert slicer.mrmlScene.GetNumberOfNodes() == before
        shortcut = widget.navigation_action.shortcut
        shortcut = shortcut() if callable(shortcut) else shortcut
        assert str(shortcut.toString()) == "Ctrl+Alt+L"
        widget.cleanup()
        slicer.app.processEvents()
        assert not [bar for bar in slicer.util.mainWindow().findChildren(qt.QToolBar)
                    if str(bar.objectName) == "LiveSegmentationNavigation"], "Toolbar survived cleanup"
        result.update(ok=True, scene_unchanged=True, return_from_another_module=True, toolbar_count=len(bars))
    except Exception as exc:
        result.update(error=str(exc), traceback=traceback.format_exc())
    Path(os.environ["LIVESEG_NAVIGATION_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    slicer.app.exit(0 if result["ok"] else 1)


qt.QTimer.singleShot(1200, run)
