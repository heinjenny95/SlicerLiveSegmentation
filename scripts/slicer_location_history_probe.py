"""Run in two fresh --disable-settings processes to verify local address recall."""
import json
import os
import traceback
from pathlib import Path

import qt
import slicer


def run():
    result = {"ok": False}
    try:
        from LiveSegmentationLib import collaboration as c
        history_root = os.environ["LIVESEG_TEST_HISTORY_ROOT"]
        if hasattr(c, "location_history_settings"):
            original = c.location_history_settings
            c.location_history_settings = lambda: original(history_root)
        slicer.util.selectModule("LiveSegmentation")
        controller = slicer.modules.livesegmentation.widgetRepresentation().self().live_collaboration
        mode = os.environ["LIVESEG_HISTORY_MODE"]
        path = "ssh://specimen.example.org/mnt/data/shared"
        if mode == "write":
            controller._remember_recent_shared_folder(path)
        else:
            paths = [str(controller.shared_folder_edit.itemText(i)) for i in range(controller.shared_folder_edit.count)]
            assert path in paths, f"Successful server address lost after restart: {paths}"
            assert not controller._text(controller.shared_folder_edit), "Startup must stay disconnected with an empty active path"
            assert not controller.connected and not controller._joining
            assert not controller._text(controller.ssh_password_edit)
        result.update(ok=True, mode=mode)
    except Exception as exc:
        result.update(error=str(exc), traceback=traceback.format_exc())
    Path(os.environ["LIVESEG_HISTORY_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    slicer.app.exit(0 if result["ok"] else 1)


qt.QTimer.singleShot(1200, run)
